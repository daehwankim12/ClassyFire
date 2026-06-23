"""Circuit breaker for the remote provider boundary (NFR-P05).

Consecutive remote failures open the circuit; while OPEN the worker stops making
remote calls (and parks tasks as ``DEFERRED``) but LOCAL lookups and queue
accumulation keep working — a Wishart outage must never stop the local pipeline.
After a cool-down the circuit goes HALF_OPEN and admits a single probe; success
closes it, failure re-opens it.

State lives in-process per worker by design: the breaker guards *this* worker's
outbound calls. A clock is injectable for deterministic tests.
"""
from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """Consecutive-failure breaker with a half-open recovery probe."""

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_timeout: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        import time

        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._clock = clock or time.monotonic
        self._failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = 0.0

    @property
    def state(self) -> CircuitState:
        # Lazily promote OPEN -> HALF_OPEN once the cool-down elapses.
        if (
            self._state == CircuitState.OPEN
            and self._clock() - self._opened_at >= self.reset_timeout
        ):
            self._state = CircuitState.HALF_OPEN
        return self._state

    def allow(self) -> bool:
        """Whether a remote call may be attempted right now."""
        return self.state != CircuitState.OPEN

    def record_success(self) -> None:
        self._failures = 0
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        # A failure during a half-open probe immediately re-opens the circuit.
        if self.state == CircuitState.HALF_OPEN:
            self._trip()
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._clock()

    @classmethod
    def from_settings(
        cls, clock: Callable[[], float] | None = None
    ) -> CircuitBreaker:
        from django.conf import settings

        return cls(
            failure_threshold=getattr(settings, "REMOTE_CIRCUIT_THRESHOLD", 5),
            reset_timeout=getattr(settings, "REMOTE_CIRCUIT_RESET_SECONDS", 60.0),
            clock=clock,
        )
