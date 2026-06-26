"""Import a portable backup bundle into the local backup store."""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Verify and stage a portable backup bundle into the backup directory."

    def add_arguments(self, parser) -> None:
        parser.add_argument("bundle")
        parser.add_argument("--backup-dir", default=None)
        parser.add_argument("--overwrite", action="store_true")

    def handle(self, *args, **options) -> None:
        from apps.operations.backup_bundle import BackupBundleError, import_backup_bundle

        backup_dir = Path(options["backup_dir"]) if options["backup_dir"] else None
        try:
            result = import_backup_bundle(
                Path(options["bundle"]),
                backup_dir=backup_dir,
                overwrite=bool(options["overwrite"]),
            )
        except BackupBundleError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(f"Imported backup manifest: {result['manifest_file']}")
        )
        self.stdout.write(f"Bundle checksum: {result['bundle_checksum']}")
        self.stdout.write(
            "Restore hint: BACKUP_DIR="
            f"{result['backup_dir']} MANIFEST_FILE={result['manifest_file']} "
            "deploy/backup/restore.sh"
        )
        self.stdout.write(json.dumps(result, indent=2))
