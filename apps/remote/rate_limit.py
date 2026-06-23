"""Global rate limiter for remote calls.

Two independent buckets — entity lookups and query submits — each honour a
per-minute budget (``REMOTE_ENTITY_REQUESTS_PER_MIN`` /
``REMOTE_SUBMITS_PER_MIN``). A server ``429`` / ``Retry-After`` takes ABSOLUTE
precedence: ``penalize(retry_after)`` parks the relevant bucket until that
deadline regardless of the local budget.

The limiter is a sliding-window counter keyed only on monotonic timestamps, so
the same algorithm is trivially portable to another language reading the same
config. A clock is injectable for deterministic tests.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

WINDOW_SECONDS = 60.0


@dataclass
class _Bucket:
    """Sliding-window limiter for one operation class."""

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

    def penalize(self, retry_after: float | None) -> None:
        """Park this bucket for ``retry_after`` seconds (server precedence)."""
        if retry_after is None or retry_after <= 0:
            return
        self._blocked_until = max(self._blocked_until, self.clock() + retry_after)


class RateLimiter:
    """Holds the entity + submit buckets."""

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
