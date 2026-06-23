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

from django.core.management.base import BaseCommand, CommandError

from apps.classifications.seed_manifest import (
    compute_sha256,
    create_seed_import_row,
    versioned_table_name,
)
from apps.classifications.seed_stream import StreamImportError, stream_import


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
        if resume and seed_import_id:
            try:
                seed_import = SeedImport.objects.get(pk=seed_import_id)
                self.stdout.write(f"Resuming SeedImport pk={seed_import_id}")
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
            target_table = vt

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
