"""Core app models.

Holds infrastructure-level state that must be shared across all gunicorn
worker processes. In particular, :class:`LoginThrottle` backs the shared-access
login failure throttle with database-resident counters (rather than the
process-local Django cache), so the throttle actually limits attempts when more
than one worker is running.
"""
from __future__ import annotations

from django.db import models


class LoginThrottle(models.Model):
    """Per-key shared counter for failed shared-access login attempts.

    Per-IP and global limits each use a distinct row. Increments are serialised with
    ``select_for_update`` so concurrent worker processes share a single,
    atomically-updated failure counter (no lost updates).
    """

    key = models.CharField(max_length=128, unique=True)
    failure_count = models.IntegerField(default=0)
    window_started_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "login_throttles"

    def __str__(self) -> str:  # pragma: no cover - debug aid
        return f"LoginThrottle(key={self.key!r}, failure_count={self.failure_count})"
