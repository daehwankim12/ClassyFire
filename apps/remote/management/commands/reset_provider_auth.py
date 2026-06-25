from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.core.enums import ProviderControlState
from apps.remote.models import RemoteProviderControl


class Command(BaseCommand):
    help = "Reset an AUTH_BLOCKED remote provider after credentials are fixed."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--provider", default="wishart")

    def handle(self, *args: Any, **options: Any) -> None:
        provider = options["provider"]
        RemoteProviderControl.objects.update_or_create(
            provider=provider,
            defaults={
                "state": ProviderControlState.ENABLED,
                "blocked_reason": "",
            },
        )
        self.stdout.write(self.style.SUCCESS(f"{provider} provider auth reset."))
