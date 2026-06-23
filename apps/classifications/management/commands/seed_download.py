"""Management command: resolve Zenodo record metadata and create SeedImport row.

The actual large-file download is NOT performed by this command — only metadata
is resolved.  Use ``seed_import --source <path>`` for the actual data import.

Offline / dry-run mode (``--dry-run``) is safe to use in tests and CI.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.classifications.seed_manifest import (
    ManifestError,
    create_seed_import_row,
    resolve_zenodo_record,
)


class Command(BaseCommand):
    help = "Resolve Zenodo seed metadata (no large download). Creates a SeedImport row."

    def add_arguments(self, parser):
        parser.add_argument(
            "--record-id",
            type=int,
            default=None,
            help="Zenodo record ID (overrides SEED_RECORD_ID setting).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Skip network call; return placeholder manifest without writing DB.",
        )

    def handle(self, *args, **options):
        record_id = options["record_id"]
        dry_run = options["dry_run"]

        try:
            manifest = resolve_zenodo_record(record_id, dry_run=dry_run)
        except ManifestError as exc:
            raise CommandError(str(exc)) from exc

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no DB changes."))
            self.stdout.write(str(manifest))
            return

        row = create_seed_import_row(manifest)
        self.stdout.write(
            self.style.SUCCESS(
                f"Created SeedImport pk={row.pk} for record {manifest['record_id']}"
            )
        )
        self.stdout.write(f"  DOI: {manifest['doi']}")
        self.stdout.write(f"  File: {manifest['filename']}")
        self.stdout.write(f"  Checksum: {manifest['checksum']}")
        self.stdout.write(f"  License: {manifest['license']}")
