from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.operations.admin_views import resume_remote


class Command(BaseCommand):
    help = "Resume remote worker task claiming."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--actor", default="admin", help="Actor recorded in SystemEvent.")

    def handle(self, *args, **options) -> None:
        event = resume_remote(actor=str(options["actor"]))
        self.stdout.write(
            self.style.SUCCESS(f"Resumed remote processing (event {event.pk}).")
        )
