"""Export an existing backup manifest set as one portable bundle."""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Package an existing backup manifest and referenced files into a tar.gz bundle."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--backup-dir", default=None)
        parser.add_argument("--manifest", default=None)
        parser.add_argument("--output", required=True)
        parser.add_argument("--overwrite", action="store_true")

    def handle(self, *args, **options) -> None:
        from apps.operations.backup_bundle import BackupBundleError, create_backup_bundle

        backup_dir = Path(options["backup_dir"]) if options["backup_dir"] else None
        try:
            result = create_backup_bundle(
                Path(options["output"]),
                backup_dir=backup_dir,
                manifest_file=options["manifest"],
                overwrite=bool(options["overwrite"]),
            )
        except BackupBundleError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f"Backup bundle complete: {result['bundle']}"))
        self.stdout.write(f"Checksum: {result['checksum']}")
        self.stdout.write(json.dumps(result, indent=2))
