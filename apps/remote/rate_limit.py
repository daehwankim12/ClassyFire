"""Rate limiting for remote calls.

Two implementations share one duck-typed interface (a limiter exposes
``.entity`` and ``.submit`` buckets, each with ``allow() -> bool`` and
``penalize(retry_after)``):

* :class:`DbRateLimiter` — the PRODUCTION limiter (F-08). A **shared, DB-backed
  token bucket** (one ``remote_rate_buckets`` row per ``(provider, bucket)``):
  tokens are drawn ATOMICALLY with ``UPDATE ... SET tokens = tokens - 1 WHERE
  tokens >= 1 RETURNING`` under a row lock, so N workers across processes
  respect ONE global rate. In production, ``from_settings()`` aliases entity
  GETs, polls, and submit POSTs to a single ``request`` bucket because Wishart
  429s on aggregate per-IP traffic. Tokens refill lazily from
  ``refill_rate_per_sec`` based on elapsed wall-clock time, capped at
  ``capacity``. A server ``Retry-After`` parks the bucket (``blocked_until``)
  with absolute precedence.

* :class:`RateLimiter` — the legacy in-process sliding-window limiter, retained
  so tests that inject a deterministic, network-free limiter keep working. It is
  NOT used as the default in production.

Both are keyed only on timestamps + config, so the algorithm is portable to a
Go/Rust worker reading the same ``remote_rate_buckets`` rows.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

WINDOW_SECONDS = 60.0


# ---------------------------------------------------------------------------
# DB-backed shared token bucket (F-08, production)
# ---------------------------------------------------------------------------


class DbTokenBucket:
    """One shared token bucket row drawn from atomically across processes."""

    def __init__(
        self,
        bucket: str,
        capacity: int,
        refill_per_min: int,
        *,
        provider: str = "wishart",
    ) -> None:
        self.bucket = bucket
        self.provider = provider
        # capacity is the burst size; default it to the per-minute budget so a
        # fresh worker can immediately use a full minute's worth of tokens.
        self.capacity = max(int(capacity), 0)
        self.refill_rate_per_sec = max(refill_per_min, 0) / WINDOW_SECONDS

    def _ensure_row(self) -> None:
        from apps.remote.models import RemoteRateBucket

        RemoteRateBucket.objects.get_or_create(
            provider=self.provider,
            bucket=self.bucket,
            defaults={
                "capacity": self.capacity,
                "tokens": float(self.capacity),
                "refill_rate_per_sec": self.refill_rate_per_sec,
            },
        )

    def allow(self) -> bool:
        """Atomically consume one token if available across ALL workers.

        Locks the single bucket row (``SELECT ... FOR UPDATE``), lazily refills
        based on elapsed time, then decrements iff a whole token is available.
        Returns whether the caller may proceed. Disabled (capacity 0 AND no
        refill) buckets always allow.
        """
        from apps.remote.models import RemoteRateBucket

        if self.capacity <= 0 and self.refill_rate_per_sec <= 0:
            return True
        self._ensure_row()
        now = timezone.now()
        with transaction.atomic():
            row = (
                RemoteRateBucket.objects.select_for_update()
                .filter(provider=self.provider, bucket=self.bucket)
                .get()
            )
            # Keep config in sync with settings (capacity/refill may change).
            row.capacity = self.capacity
            row.refill_rate_per_sec = self.refill_rate_per_sec
            if row.blocked_until is not None and now < row.blocked_until:
                row.save(update_fields=["capacity", "refill_rate_per_sec"])
                return False
            elapsed = max((now - row.updated_at).total_seconds(), 0.0)
            refilled = min(
                float(row.capacity),
                row.tokens + elapsed * row.refill_rate_per_sec,
            )
            if refilled >= 1.0:
                row.tokens = refilled - 1.0
                row.save(
                    update_fields=[
                        "tokens",
                        "capacity",
                        "refill_rate_per_sec",
                        "updated_at",
                    ]
                )
                return True
            row.tokens = refilled
            row.save(
                update_fields=[
                    "tokens",
                    "capacity",
                    "refill_rate_per_sec",
                    "updated_at",
                ]
            )
            return False

    def retry_after_seconds(self) -> float:
        """Seconds until the next token/probe should be available.

        Used by queue workers after an ``allow()`` miss so a smooth capacity-1
        bucket is retried at its refill cadence instead of an unrelated fixed
        delay. ``Retry-After`` blocks retain absolute precedence.
        """
        from apps.remote.models import RemoteRateBucket

        if self.capacity <= 0 and self.refill_rate_per_sec <= 0:
            return 0.0
        self._ensure_row()
        now = timezone.now()
        row = RemoteRateBucket.objects.filter(
            provider=self.provider, bucket=self.bucket
        ).get()
        if row.blocked_until is not None and now < row.blocked_until:
            return max((row.blocked_until - now).total_seconds(), 0.0)
        elapsed = max((now - row.updated_at).total_seconds(), 0.0)
        refilled = min(
            float(row.capacity),
            row.tokens + elapsed * row.refill_rate_per_sec,
        )
        if refilled >= 1.0 or row.refill_rate_per_sec <= 0:
            return 0.0
        return max((1.0 - refilled) / row.refill_rate_per_sec, 0.0)

    def penalize(self, retry_after: float | None) -> None:
        """Park the shared bucket until ``now + retry_after`` (server precedence)."""
        if retry_after is None or retry_after <= 0:
            return
        from apps.remote.models import RemoteRateBucket

        self._ensure_row()
        deadline = timezone.now() + timedelta(seconds=retry_after)
        with transaction.atomic():
            row = (
                RemoteRateBucket.objects.select_for_update()
                .filter(provider=self.provider, bucket=self.bucket)
                .get()
            )
            if row.blocked_until is None or deadline > row.blocked_until:
                row.blocked_until = deadline
                row.save(update_fields=["blocked_until", "updated_at"])


class DbRateLimiter:
    """Holds DB-backed entity + submit token buckets.

    Direct construction keeps separate buckets for targeted tests and injected
    limiters. ``from_settings()`` is the production path and deliberately aliases
    both operations to one request bucket.
    """

    def __init__(
        self,
        entity_per_min: int,
        submit_per_min: int,
        *,
        provider: str = "wishart",
        entity_capacity: int | None = None,
        submit_capacity: int | None = None,
    ) -> None:
        self.entity = DbTokenBucket(
            "entity",
            entity_capacity if entity_capacity is not None else entity_per_min,
            entity_per_min,
            provider=provider,
        )
        self.submit = DbTokenBucket(
            "submit",
            submit_capacity if submit_capacity is not None else submit_per_min,
            submit_per_min,
            provider=provider,
        )

    @classmethod
    def from_settings(cls, provider: str = "wishart") -> DbRateLimiter:
        from django.conf import settings

        request_per_min = min(
            settings.REMOTE_ENTITY_REQUESTS_PER_MIN,
            settings.REMOTE_SUBMITS_PER_MIN,
        )
        # Wishart's live 429 behaviour is IP-wide in practice: entity GETs,
        # query polls, and query POSTs all draw from the same upstream allowance.
        # Use one shared bucket so a POST cannot race past GET/poll traffic and
        # push the aggregate request rate over the provider cap.
        request_bucket = DbTokenBucket(
            "request",
            capacity=1,
            refill_per_min=request_per_min,
            provider=provider,
        )
        limiter = cls(
            entity_per_min=0,
            submit_per_min=0,
            entity_capacity=0,
            submit_capacity=0,
            provider=provider,
        )
        limiter.entity = request_bucket
        limiter.submit = request_bucket
        return limiter


# ---------------------------------------------------------------------------
# Legacy in-process sliding-window limiter (tests / deterministic injection)
# ---------------------------------------------------------------------------


@dataclass
class _Bucket:
    """Sliding-window limiter for one operation class (in-process)."""

    limit_per_min: int
    clock: Callable[[], float]
    _events: deque[float] = field(default_factory=deque)
    _blocked_until: float = 0.0

    def _evict(self, now: float) -> None:
        cutoff = now - WINDOW_SECONDS
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()

    def retry_at(self) -> float | None:
        """Earliest time a call is allowed, or ``None`` if allowed now."""
        now = self.clock()
        if now < self._blocked_until:
            return self._blocked_until
        if self.limit_per_min <= 0:
            return None
        self._evict(now)
        if len(self._events) < self.limit_per_min:
            return None
        # Window full: next slot frees when the oldest event ages out.
        return self._events[0] + WINDOW_SECONDS

    def allow(self) -> bool:
        """Consume a slot if available; return whether the call may proceed."""
        if self.retry_at() is not None:
            return False
        self._events.append(self.clock())
        return True

    def retry_after_seconds(self) -> float:
        """Seconds until this in-process bucket should be retried."""
        retry_at = self.retry_at()
        if retry_at is None:
            return 0.0
        return max(retry_at - self.clock(), 0.0)

    def penalize(self, retry_after: float | None) -> None:
        """Park this bucket for ``retry_after`` seconds (server precedence)."""
        if retry_after is None or retry_after <= 0:
            return
        self._blocked_until = max(self._blocked_until, self.clock() + retry_after)


class RateLimiter:
    """Holds the entity + submit buckets (legacy in-process)."""

    def __init__(
        self,
        entity_per_min: int,
        submit_per_min: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        import time

        self._clock = clock or time.monotonic
        self.entity = _Bucket(entity_per_min, self._clock)
        self.submit = _Bucket(submit_per_min, self._clock)

    @classmethod
    def from_settings(cls, clock: Callable[[], float] | None = None) -> RateLimiter:
        from django.conf import settings

        return cls(
            entity_per_min=settings.REMOTE_ENTITY_REQUESTS_PER_MIN,
            submit_per_min=settings.REMOTE_SUBMITS_PER_MIN,
            clock=clock,
        )
