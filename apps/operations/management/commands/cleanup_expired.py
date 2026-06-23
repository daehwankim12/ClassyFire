"""Management command: clean up expired uploads, exports, and job secrets."""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Delete expired uploads/exports and wipe old job secrets."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--actor", default="management_command")

    def handle(self, *args, **options) -> None:
        from apps.operations.cleanup import cleanup_expired

        result = cleanup_expired(actor=options["actor"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Cleanup done: secrets_wiped={result.jobs_secrets_wiped}, "
                f"jobs_deleted={result.jobs_deleted}, "
                f"exports_deleted={result.exports_deleted}"
            )
        )
