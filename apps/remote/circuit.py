"""Circuit breaker for the remote provider boundary (NFR-P05, F-08).

Consecutive remote failures open the circuit; while OPEN the worker stops making
remote calls (and parks tasks as ``DEFERRED``) but LOCAL lookups and queue
accumulation keep working — a Wishart outage must never stop the local pipeline.
After a cool-down the circuit goes HALF_OPEN and admits a SINGLE probe; success
closes it, failure re-opens it.

Two implementations share one duck-typed interface (``allow``, ``record_success``,
``record_failure``, ``state``):

* :class:`DbCircuitBreaker` — the PRODUCTION breaker (F-08). State lives in a
  shared ``remote_circuits`` row so N workers across processes share ONE breaker.
  When OPEN→HALF_OPEN only ONE worker wins the probe via a CONDITIONAL update
  (``WHERE state='OPEN' AND opened_at <= cutoff`` flips exactly one row to
  HALF_OPEN); the rest still see OPEN and defer — no thundering herd.

* :class:`CircuitBreaker` — the legacy in-process breaker, retained so tests can
  inject a deterministic, network-free breaker. NOT the production default.

``CircuitState`` is re-exported from :mod:`apps.core.enums` (the canonical,
language-agnostic ``TextChoices``) so the persisted DB value and the in-process
enum are the SAME string literals.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from apps.core.enums import CircuitState

if TYPE_CHECKING:
    from apps.remote.models import RemoteCircuit

__all__ = ["CircuitBreaker", "CircuitState", "DbCircuitBreaker"]


# ---------------------------------------------------------------------------
# DB-backed shared circuit breaker (F-08, production)
# ---------------------------------------------------------------------------


class DbCircuitBreaker:
    """Shared, DB-backed consecutive-failure breaker (one row per provider)."""

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_timeout: float = 60.0,
        *,
        provider: str = "wishart",
    ) -> None:
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.provider = provider

    def _row(self) -> RemoteCircuit:
        from apps.remote.models import RemoteCircuit

        row, _ = RemoteCircuit.objects.get_or_create(provider=self.provider)
        return row

    @property
    def state(self) -> CircuitState:
        """Current shared state as persisted (no side effects).

        Reads the row without attempting the half-open promotion — call
        :meth:`allow` to attempt (and win) the single probe. The state DOES
        lazily report HALF_OPEN once the cool-down has elapsed so observers can
        see the circuit is eligible to probe.
        """
        from datetime import timedelta

        from django.utils import timezone

        row = self._row()
        if (
            row.state == CircuitState.OPEN
            and row.opened_at is not None
            and row.opened_at <= timezone.now() - timedelta(seconds=self.reset_timeout)
        ):
            return CircuitState.HALF_OPEN
        return CircuitState(row.state)

    def allow(self) -> bool:
        """Whether THIS worker may attempt a remote call right now.

        * CLOSED → always True.
        * OPEN, cool-down NOT elapsed → False.
        * OPEN, cool-down elapsed → exactly ONE worker wins the probe via a
          CONDITIONAL ``UPDATE ... WHERE state='OPEN'`` flip to HALF_OPEN and
          gets True; every other worker sees HALF_OPEN (probe in flight) → False.
        * HALF_OPEN (a probe is already in flight) → False (no thundering herd).
        """
        from datetime import timedelta

        from django.utils import timezone

        from apps.remote.models import RemoteCircuit

        row = self._row()
        if row.state == CircuitState.CLOSED:
            return True
        if row.state == CircuitState.HALF_OPEN:
            # A probe is already in flight; this worker must not pile on.
            return False
        # OPEN: only the cool-down winner may probe.
        if row.opened_at is None:
            return False
        cutoff = timezone.now() - timedelta(seconds=self.reset_timeout)
        if row.opened_at > cutoff:
            return False
        won = RemoteCircuit.objects.filter(
            provider=self.provider,
            state=CircuitState.OPEN,
            opened_at__lte=cutoff,
        ).update(state=CircuitState.HALF_OPEN)
        return bool(won)

    def record_success(self) -> None:
        from apps.remote.models import RemoteCircuit

        RemoteCircuit.objects.update_or_create(
            provider=self.provider,
            defaults={
                "state": CircuitState.CLOSED,
                "consecutive_failures": 0,
                "opened_at": None,
            },
        )

    def record_failure(self) -> None:
        from django.db import transaction
        from django.db.models import F
        from django.utils import timezone

        from apps.remote.models import RemoteCircuit

        with transaction.atomic():
            row = (
                RemoteCircuit.objects.select_for_update()
                .get_or_create(provider=self.provider)[0]
            )
            if row.state == CircuitState.HALF_OPEN:
                # A failure during the half-open probe immediately re-opens.
                row.state = CircuitState.OPEN
                row.opened_at = timezone.now()
                row.save(update_fields=["state", "opened_at", "updated_at"])
                return
            RemoteCircuit.objects.filter(pk=row.pk).update(
                consecutive_failures=F("consecutive_failures") + 1
            )
            row.refresh_from_db()
            if row.consecutive_failures >= self.failure_threshold:
                row.state = CircuitState.OPEN
                row.opened_at = timezone.now()
                row.save(update_fields=["state", "opened_at", "updated_at"])

    @classmethod
    def from_settings(cls, provider: str = "wishart") -> DbCircuitBreaker:
        from django.conf import settings

        return cls(
            failure_threshold=getattr(settings, "REMOTE_CIRCUIT_THRESHOLD", 5),
            reset_timeout=getattr(settings, "REMOTE_CIRCUIT_RESET_SECONDS", 60.0),
            provider=provider,
        )


# ---------------------------------------------------------------------------
# Legacy in-process circuit breaker (tests / deterministic injection)
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Consecutive-failure breaker with a half-open recovery probe (in-process)."""

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
