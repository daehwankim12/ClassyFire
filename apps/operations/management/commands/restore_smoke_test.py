"""Management command: restore latest backup into a smoke-test DB and verify."""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Restore the latest backup into a separate DB and run a smoke check."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--backup-dir", default=None)
        parser.add_argument(
            "--target-db",
            default=None,
            help="Target DB name (default: settings.RESTORE_SMOKE_DB or classyfire_smoke_test).",
        )

    def handle(self, *args, **options) -> None:
        from apps.operations.backup import restore_smoke_test

        backup_dir = Path(options["backup_dir"]) if options["backup_dir"] else None
        target_db = options.get("target_db")

        result = restore_smoke_test(backup_dir=backup_dir, target_db=target_db)

        if result.get("status") == "ok":
            self.stdout.write(self.style.SUCCESS(f"Smoke test passed: {result}"))
        else:
            self.stderr.write(self.style.ERROR(f"Smoke test failed: {result}"))
