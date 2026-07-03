from __future__ import annotations

from collections.abc import Iterable

from django.db import transaction
from django.utils import timezone

from apps.remote.models import RemoteNegativeCache

NEGATIVE_HARD_FAILED_MESSAGE = (
    "Wishart negative cache hard-failed for this InChIKey."
)
NEGATIVE_FAILURE_THRESHOLD = 3


def record_negative_failure(
    inchikey: str,
    *,
    operation: str,
    http_status: int | None,
    detail: str | None,
) -> RemoteNegativeCache:
    """Record a failed Wishart submit/poll path for one InChIKey."""
    now = timezone.now()
    with transaction.atomic():
        cache, _ = RemoteNegativeCache.objects.select_for_update().get_or_create(
            inchikey=inchikey,
            defaults={
                "state": RemoteNegativeCache.State.CANDIDATE,
                "failure_count": 0,
            },
        )
        if cache.state == RemoteNegativeCache.State.RESOLVED:
            cache.failure_count = 0
            cache.state = RemoteNegativeCache.State.CANDIDATE
        cache.failure_count += 1
        cache.last_failure_operation = operation
        cache.last_http_status = http_status
        cache.last_detail = detail
        cache.last_failed_at = now
        cache.next_recheck_at = now
        if cache.failure_count >= NEGATIVE_FAILURE_THRESHOLD:
            cache.state = RemoteNegativeCache.State.HARD_FAILED
        cache.save(
            update_fields=[
                "state",
                "failure_count",
                "last_failure_operation",
                "last_http_status",
                "last_detail",
                "last_failed_at",
                "next_recheck_at",
                "updated_at",
            ]
        )
        return cache


def mark_negative_resolved_from_wishart(inchikey: str) -> None:
    """Close negative state after a verified Wishart result reaches live cache."""
    from apps.classifications.services import lookup_current_classification

    result = lookup_current_classification(inchikey)
    if result is None:
        return
    RemoteNegativeCache.objects.filter(inchikey=inchikey).update(
        state=RemoteNegativeCache.State.RESOLVED,
        next_recheck_at=None,
        updated_at=timezone.now(),
    )


def reset_negative_for_recheck(
    negative_cache: RemoteNegativeCache, *, actor: str | None = None
) -> RemoteNegativeCache:
    """Allow operators to force a fresh Wishart recheck for a hard failure."""
    negative_cache.state = RemoteNegativeCache.State.CANDIDATE
    negative_cache.failure_count = 0
    negative_cache.next_recheck_at = timezone.now()
    negative_cache.last_detail = (
        f"manual recheck requested by {actor}" if actor else "manual recheck requested"
    )
    negative_cache.save(
        update_fields=[
            "state",
            "failure_count",
            "next_recheck_at",
            "last_detail",
            "updated_at",
        ]
    )
    return negative_cache


def hard_negative_inchikeys(inchikeys: Iterable[str]) -> set[str]:
    keys = list(dict.fromkeys(inchikeys))
    if not keys:
        return set()
    return set(
        RemoteNegativeCache.objects.filter(
            inchikey__in=keys,
            state=RemoteNegativeCache.State.HARD_FAILED,
        ).values_list("inchikey", flat=True)
    )
