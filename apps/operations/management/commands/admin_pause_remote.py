from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.operations.admin_views import pause_remote


class Command(BaseCommand):
    help = "Pause remote worker task claiming."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--actor", default="admin", help="Actor recorded in SystemEvent.")

    def handle(self, *args, **options) -> None:
        event = pause_remote(actor=str(options["actor"]))
        self.stdout.write(
            self.style.SUCCESS(f"Paused remote processing (event {event.pk}).")
        )
