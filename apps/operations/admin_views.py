"""Admin operations: retry tasks, remote pause/resume, backup status.

All state is DB-backed (SystemEvent rows or model fields) so a future Go/Rust
operator tool can read/write the same state.

Remote pause flag
-----------------
Implemented as an event-sourced flag: the worker checks whether the latest
SystemEvent with event_type in {"remote_pause", "remote_resume"} is a pause.
This is language-agnostic — any worker in any language queries:

    SELECT event_type FROM system_events
    WHERE category = 'WORKER'
      AND event_type IN ('remote_pause', 'remote_resume')
    ORDER BY created_at DESC LIMIT 1;

If the result is 'remote_pause' the worker should not claim tasks.
If no row exists the default is running (not paused) — workers may claim tasks.
"""
from __future__ import annotations

from django.utils import timezone

from apps.core.enums import EventCategory, TaskState
from apps.operations.models import SystemEvent

# ---------------------------------------------------------------------------
# Remote pause / resume
# ---------------------------------------------------------------------------

_PAUSE_EVENT = "remote_pause"
_RESUME_EVENT = "remote_resume"


def is_remote_paused() -> bool:
    """Return True only if the most recent pause/resume event is a pause.

    Returns False when no event exists (default = running / not paused).
    In SQL terms: SELECT event_type FROM system_events WHERE category='WORKER'
    AND event_type IN ('remote_pause','remote_resume') ORDER BY created_at DESC
    LIMIT 1 — if the row is 'remote_pause' → paused; 'remote_resume' or no row
    → not paused.
    """
    event = (
        SystemEvent.objects.filter(
            category=EventCategory.WORKER,
            event_type__in=[_PAUSE_EVENT, _RESUME_EVENT],
        )
        .order_by("-created_at")
        .first()
    )
    if event is None:
        return False
    return event.event_type == _PAUSE_EVENT


def pause_remote(actor: str = "admin") -> SystemEvent:
    """Pause remote task processing and log the event."""
    ev = SystemEvent.objects.create(
        category=EventCategory.WORKER,
        event_type=_PAUSE_EVENT,
        payload={"actor": actor},
    )
    return ev


def resume_remote(actor: str = "admin") -> SystemEvent:
    """Resume remote task processing and log the event."""
    ev = SystemEvent.objects.create(
        category=EventCategory.WORKER,
        event_type=_RESUME_EVENT,
        payload={"actor": actor},
    )
    return ev


# ---------------------------------------------------------------------------
# Retry failed / deferred tasks
# ---------------------------------------------------------------------------

def retry_failed_tasks(actor: str = "admin") -> int:
    """Reset DEFERRED and RETRY_WAIT remote tasks (the backoff-parked states) to READY for immediate retry.

    DEFERRED = operator-parked; RETRY_WAIT = exponential-backoff pause.
    Both are safe to force-reset because they are not currently held by a worker.
    In-flight states (LOCKED, RUNNING, ENTITY_LOOKUP, SUBMIT_WAIT, SUBMITTED,
    POLL_WAIT) must NOT be force-reset. DONE is terminal/success and is excluded.
    Note: TaskState has no FAILED value — that exists only on QueryState.

    Returns number of tasks reset.
    """
    from apps.remote.models import RemoteTask

    now = timezone.now()
    qs = RemoteTask.objects.filter(state__in=[TaskState.DEFERRED, TaskState.RETRY_WAIT])
    count = qs.update(state=TaskState.READY, next_run_at=now, lock_owner=None, locked_at=None)

    SystemEvent.objects.create(
        category=EventCategory.WORKER,
        event_type="retry_deferred_tasks",
        payload={"count": count, "actor": actor},
    )
    return count


# ---------------------------------------------------------------------------
# Backup status review
# ---------------------------------------------------------------------------

def get_backup_status() -> dict[str, object]:
    """Return a summary of the latest backup status from system_events."""
    ev = (
        SystemEvent.objects.filter(category=EventCategory.BACKUP, event_type="backup_complete")
        .order_by("-created_at")
        .first()
    )
    if ev is None:
        return {"status": "never", "latest_backup_at": None}
    return {
        "status": ev.payload.get("status", "ok"),
        "latest_backup_at": ev.created_at.isoformat(),
        "file": ev.payload.get("file"),
        "checksum": ev.payload.get("checksum"),
    }
