from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.remote.services import reset_provider_auth


class Command(BaseCommand):
    help = "Reset an AUTH_BLOCKED remote provider after credentials are fixed."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--provider", default="wishart")
        parser.add_argument(
            "--actor",
            default="reset_provider_auth",
            help="Actor recorded in the SystemEvent audit row.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        provider = options["provider"]
        reset_provider_auth(provider=provider, actor=str(options["actor"]))
        self.stdout.write(self.style.SUCCESS(f"{provider} provider auth reset."))
