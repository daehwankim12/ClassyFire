"""Management command: run a database backup."""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Run a pg_dump backup with checksum, manifest, and retention cleanup."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--backup-dir",
            default=None,
            help="Directory to write backups (default: settings.BACKUP_DIR or /backups).",
        )

    def handle(self, *args, **options) -> None:
        from apps.operations.backup import run_backup

        backup_dir = Path(options["backup_dir"]) if options["backup_dir"] else None
        try:
            manifest = run_backup(backup_dir=backup_dir)
        except Exception as exc:
            raise CommandError(f"Backup failed: {exc}") from exc

        self.stdout.write(self.style.SUCCESS(f"Backup complete: {manifest['file']}"))
        self.stdout.write(f"Checksum: {manifest['checksum']}")
        self.stdout.write(json.dumps(manifest, indent=2))
