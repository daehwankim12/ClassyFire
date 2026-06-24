from __future__ import annotations

from datetime import datetime, time

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.remote.models import RemoteBudgetLedger, RemoteTask


class RemoteBudgetExceededError(ValueError):
    """Raised when a principal has exhausted the configured remote budget."""


def normalize_principal(principal: str | None) -> str:
    value = (principal or "system").strip()
    return value[:128] if value else "system"


def _daily_window_start():
    now = timezone.localtime(timezone.now())
    return timezone.make_aware(
        datetime.combine(now.date(), time.min),
        timezone.get_current_timezone(),
    )


def reserve_remote_budget(
    principal: str | None,
    *,
    task: RemoteTask | None = None,
    units: int = 1,
    reason: str = "remote_task",
) -> RemoteBudgetLedger | None:
    """Reserve remote-budget units for a principal.

    A negative ``REMOTE_DAILY_BUDGET_PER_PRINCIPAL`` disables enforcement and
    ledger writes, preserving the historic unlimited behavior.
    """
    if units <= 0:
        raise ValueError("remote budget units must be positive")

    limit = int(getattr(settings, "REMOTE_DAILY_BUDGET_PER_PRINCIPAL", -1))
    if limit < 0:
        return None

    principal = normalize_principal(principal)
    window_start = _daily_window_start()
    with transaction.atomic():
        used = (
            RemoteBudgetLedger.objects.filter(
                principal=principal,
                created_at__gte=window_start,
            ).aggregate(total=Sum("units"))["total"]
            or 0
        )
        if used + units > limit:
            raise RemoteBudgetExceededError(principal)
        return RemoteBudgetLedger.objects.create(
            principal=principal,
            units=units,
            reason=reason,
            task=task,
        )
