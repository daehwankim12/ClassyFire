from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.remote.models import RemoteNegativeCache

NEGATIVE_HARD_FAILED_MESSAGE = (
    "Wishart negative cache hard-failed for this InChIKey."
)
NEGATIVE_FAILURE_WINDOW = timedelta(hours=1)
# Consecutive DEFINITIVE misses (the provider completed a query and returned no
# entity for this key) before the key becomes a hard negative.
NEGATIVE_FAILURE_THRESHOLD = 3
# Detail recorded for a definitive miss; also marks the streak in last_detail.
MISSING_FROM_COMPLETE_DETAIL = "missing from completed provider response"
DEFAULT_NEGATIVE_RECHECK_HOURS = 24


def _negative_recheck_delay() -> timedelta:
    hours = getattr(settings, "REMOTE_NEGATIVE_RECHECK_HOURS", DEFAULT_NEGATIVE_RECHECK_HOURS)
    return timedelta(hours=max(float(hours), 0.0))


def record_negative_failure(
    inchikey: str,
    *,
    operation: str,
    http_status: int | None,
    detail: str | None,
    definitive: bool = False,
) -> RemoteNegativeCache:
    """Record a Wishart failure for ``inchikey``.

    Transient failures (retryable 5xx/429, protocol errors, incomplete results)
    only accumulate as a decaying CANDIDATE count: a provider outage must never
    turn into a cross-user denial. A ``definitive`` failure — a COMPLETE
    provider response that simply has no entity for this key — is a real "bad
    key" signal. NEGATIVE_FAILURE_THRESHOLD consecutive definitive misses
    within the window make the key a HARD_FAILED negative, but only until its
    bounded recheck deadline (REMOTE_NEGATIVE_RECHECK_HOURS), after which new
    jobs may check it again. This keeps provider load from known-bad keys
    bounded without letting transient errors block keys indefinitely.
    """
    now = timezone.now()
    with transaction.atomic():
        cache, _ = RemoteNegativeCache.objects.select_for_update().get_or_create(
            inchikey=inchikey,
            defaults={
                "state": RemoteNegativeCache.State.CANDIDATE,
                "failure_count": 0,
            },
        )
        if (
            cache.state == RemoteNegativeCache.State.HARD_FAILED
            and cache.next_recheck_at is not None
            and cache.next_recheck_at > now
        ):
            return cache
        if (
            cache.state != RemoteNegativeCache.State.CANDIDATE
            or cache.last_failed_at is None
            or cache.last_failed_at <= now - NEGATIVE_FAILURE_WINDOW
        ):
            cache.failure_count = 0
            cache.state = RemoteNegativeCache.State.CANDIDATE
        elif definitive and cache.last_detail != MISSING_FROM_COMPLETE_DETAIL:
            # Only an unbroken streak of definitive misses escalates; earlier
            # transient failures must not count towards a hard negative.
            cache.failure_count = 0
        cache.failure_count += 1
        cache.last_failure_operation = operation
        cache.last_http_status = http_status
        cache.last_detail = MISSING_FROM_COMPLETE_DETAIL if definitive else detail
        cache.last_failed_at = now
        if definitive and cache.failure_count >= NEGATIVE_FAILURE_THRESHOLD:
            cache.state = RemoteNegativeCache.State.HARD_FAILED
            cache.next_recheck_at = now + _negative_recheck_delay()
        else:
            cache.next_recheck_at = now
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
    """Return hard negatives whose bounded recheck deadline has not elapsed."""
    keys = list(dict.fromkeys(inchikeys))
    if not keys:
        return set()
    now = timezone.now()
    return set(
        RemoteNegativeCache.objects.filter(
            inchikey__in=keys,
            state=RemoteNegativeCache.State.HARD_FAILED,
            next_recheck_at__gt=now,
        ).values_list("inchikey", flat=True)
    )
