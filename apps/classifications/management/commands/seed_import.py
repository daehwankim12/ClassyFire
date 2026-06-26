"""Management command: stream-import a seed .tsv.zst file.

Usage examples::

    python manage.py seed_import \\
        --profile LITE \\
        --source tests/fixtures/seed_sample.tsv.zst \\
        --resume

    python manage.py seed_import \\
        --profile LITE \\
        --source tests/fixtures/seed_sample.tsv.zst \\
        --dry-run
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg.sql as pgsql
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from apps.classifications.seed_manifest import (
    compute_sha256,
    create_seed_import_row,
    versioned_table_name,
)
from apps.classifications.seed_stream import StreamImportError, stream_import


def _ensure_seed_table(table_name: str) -> None:
    """Create a versioned seed table with the same shape as classyfire_seed.

    ``LIKE classyfire_seed INCLUDING ALL`` copies the CHECK constraints, NOT NULL
    flags and column storage we want — but because ``classyfire_seed``'s primary
    key is ``inchikey``, it ALSO emits a *second* btree on ``inchikey`` next to
    the primary-key index. That duplicate is never used for reads yet costs a
    full extra copy of the key (~3 GB on a 70M-row version table) and slows every
    import by maintaining two identical indexes. We therefore exclude the copied
    indexes and recreate only what the workload uses, with no duplicate: the
    ``inchikey`` primary key and the five ChemOnt slot FK indexes (the latter
    used by ``seed_audit``'s ChemOnt validation). The ``inchikey14`` skeleton
    index is deliberately omitted — it measured 0 index scans in production while
    the inchikey PK took every lookup; the len14 CHECK constraint (copied by
    LIKE) still applies. Re-add that index only if 14-char connectivity-skeleton
    matching is ever introduced.

    Idempotent for ``--resume``: if the table already exists we leave it
    untouched, because re-running ``ADD PRIMARY KEY`` / ``CREATE INDEX`` would
    error or stack duplicate indexes.
    """
    with connection.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", [table_name])
        if cur.fetchone()[0] is not None:
            return  # already created on a previous (possibly interrupted) run

        tbl = pgsql.Identifier(table_name)
        cur.execute(
            pgsql.SQL(
                "CREATE TABLE {} (LIKE classyfire_seed INCLUDING ALL EXCLUDING INDEXES)"
            ).format(tbl)
        )
        cur.execute(pgsql.SQL("ALTER TABLE {} ADD PRIMARY KEY (inchikey)").format(tbl))
        for column in (
            "kingdom_id",
            "superclass_id",
            "class_id",
            "subclass_id",
            "direct_parent_id",
        ):
            cur.execute(
                pgsql.SQL("CREATE INDEX ON {} ({})").format(
                    tbl, pgsql.Identifier(column)
                )
            )


class Command(BaseCommand):
    help = "Stream-import a zstd-compressed seed TSV into the DB."

    def add_arguments(self, parser):
        parser.add_argument(
            "--profile",
            choices=["FULL", "LITE", "CUSTOM"],
            default="LITE",
            help="Seed profile (FULL/LITE/CUSTOM).",
        )
        parser.add_argument(
            "--source",
            required=True,
            help="Path to the local .tsv.zst file.",
        )
        parser.add_argument(
            "--seed-version",
            default=None,
            help="Version string for this seed (defaults to filename stem).",
        )
        parser.add_argument(
            "--expected-rows",
            type=int,
            default=None,
            help="Expected row count for manifest validation.",
        )
        parser.add_argument(
            "--resume",
            action="store_true",
            default=False,
            help="Resume from last checkpoint (reuse existing PENDING/IMPORTED SeedImport).",
        )
        parser.add_argument(
            "--seed-import-id",
            type=int,
            default=None,
            help="Reuse an existing SeedImport row PK (for --resume).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Parse and validate without writing rows.",
        )
        parser.add_argument(
            "--target-table",
            default="classyfire_seed",
            help="Physical table to write into (default classyfire_seed).",
        )

    def handle(self, *args, **options):
        source = Path(options["source"])
        if not source.exists():
            raise CommandError(f"Source file not found: {source}")

        profile = options["profile"]
        dry_run = options["dry_run"]
        resume = options["resume"]
        seed_version = options["seed_version"] or source.stem.replace(".tsv", "")
        target_table = options["target_table"]

        # Compute checksum.
        self.stdout.write(f"Computing checksum for {source}...")
        checksum = compute_sha256(source)
        self.stdout.write(f"SHA-256: {checksum}")

        # Find or create SeedImport row.
        from apps.operations.models import SeedImport

        seed_import_id = options["seed_import_id"]
        if seed_import_id:
            try:
                seed_import = SeedImport.objects.get(pk=seed_import_id)
                action = "Resuming" if resume else "Using"
                self.stdout.write(f"{action} SeedImport pk={seed_import_id}")
            except SeedImport.DoesNotExist as exc:
                raise CommandError(f"SeedImport {seed_import_id} not found") from exc
        elif resume:
            # Find the most recent non-terminal SeedImport for this file.
            existing = SeedImport.objects.filter(
                filename=source.name,
                import_status__in=["PENDING", "IMPORTED"],
            ).order_by("-created_at").first()
            if existing:
                seed_import = existing
                seed_import_id = existing.pk
                self.stdout.write(f"Resuming SeedImport pk={seed_import_id}")
            else:
                manifest = {
                    "doi": None,
                    "record_id": str(options.get("record_id", "")),
                    "filename": source.name,
                    "checksum": checksum,
                    "license": None,
                    "expected_row_count": options["expected_rows"],
                }
                seed_import = create_seed_import_row(manifest)
                seed_import_id = seed_import.pk
                self.stdout.write(f"Created SeedImport pk={seed_import_id}")
        else:
            manifest = {
                "doi": None,
                "record_id": os.environ.get("SEED_RECORD_ID", ""),
                "filename": source.name,
                "checksum": checksum,
                "license": None,
                "expected_row_count": options["expected_rows"],
            }
            seed_import = create_seed_import_row(manifest)
            seed_import_id = seed_import.pk
            self.stdout.write(f"Created SeedImport pk={seed_import_id}")

        # For versioned approach: target_table = classyfire_seed_v<N> for non-LITE.
        if profile == "FULL" and not dry_run and target_table == "classyfire_seed":
            vt = versioned_table_name(seed_import_id)
            self.stdout.write(f"FULL profile: will write to versioned table {vt}")
            _ensure_seed_table(vt)
            target_table = vt
            seed_import.activated_table = vt
            seed_import.save(update_fields=["activated_table"])

        self.stdout.write(
            f"Importing {source} (profile={profile}, target={target_table})..."
        )

        try:
            result = stream_import(
                source,
                seed_import_id,
                seed_version,
                target_table=target_table,
                resume=resume,
                dry_run=dry_run,
            )
        except (FileNotFoundError, StreamImportError) as exc:
            raise CommandError(str(exc)) from exc

        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"DRY RUN complete: {result}")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Import complete: rows_written={result['rows_written']}, "
                    f"skipped={result['rows_skipped']}, dupes={result['duplicates']}"
                )
            )
