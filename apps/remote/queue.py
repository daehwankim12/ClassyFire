"""The language-agnostic Postgres work queue over ``remote_tasks``.

All queue state lives in DB columns; this module just implements the protocol
documented in ``docs/state-machines.md`` §2 with Django's ORM:

* :func:`claim_task` — ``SELECT ... FOR UPDATE SKIP LOCKED`` claim of the
  highest-priority due READY task, atomically flipped to LOCKED with
  ``lock_owner``/``locked_at`` set.
* :func:`recover_stale_locks` — lease/crash recovery: LOCKED/RUNNING rows whose
  ``locked_at`` is older than the TTL are reset to READY.
* :func:`schedule_retry` / :func:`defer_task` — backoff + park transitions that
  set ``next_run_at``.
* :func:`enqueue_task` — create the single active task per InChIKey (the
  partial-unique index enforces global dedupe).

The backoff schedule and jitter are deterministic (no ``random``): jitter is
derived from ``attempt`` + a hash of the InChIKey so any-language worker can
reproduce it.
"""
from __future__ import annotations

import hashlib
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.enums import TaskState
from apps.remote.models import RemoteTask

# Backoff schedule in seconds: 1m, 5m, 30m, 2h, 6h, 24h. Past the end of the
# schedule the task is DEFERRED (parked, retried ~daily), never discarded.
BACKOFF_SCHEDULE_SECONDS: tuple[int, ...] = (
    60,
    5 * 60,
    30 * 60,
    2 * 60 * 60,
    6 * 60 * 60,
    24 * 60 * 60,
)
# Max retry attempts before a task is parked as DEFERRED.
MAX_ATTEMPTS = len(BACKOFF_SCHEDULE_SECONDS)


def jitter_fraction(attempt: int, inchikey: str) -> float:
    """Deterministic jitter in [0, 1) derived from attempt + inchikey hash.

    No ``random``: a Go/Rust worker can reproduce the same value from the same
    inputs (sha256, first 4 bytes, divided by 2**32).
    """
    digest = hashlib.sha256(f"{inchikey}:{attempt}".encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**32


def backoff_seconds(attempt: int, inchikey: str) -> float:
    """Backoff delay for ``attempt`` (0-based) with up-to-+25% deterministic jitter."""
    idx = min(attempt, len(BACKOFF_SCHEDULE_SECONDS) - 1)
    base = BACKOFF_SCHEDULE_SECONDS[idx]
    return base * (1.0 + 0.25 * jitter_fraction(attempt, inchikey))


def enqueue_task(
    inchikey: str, *, compound_id: int | None = None, priority: int = 0
) -> RemoteTask | None:
    """Create the single active RemoteTask for ``inchikey`` (idempotent).

    Returns the existing active task if one already holds the dedup slot, the
    newly created task otherwise. The partial-unique index makes this race-safe.
    """
    existing = RemoteTask.objects.filter(
        inchikey=inchikey, state__in=[s.value for s in TaskState.active_states()]
    ).first()
    if existing is not None:
        return existing
    try:
        with transaction.atomic():
            return RemoteTask.objects.create(
                inchikey=inchikey,
                compound_id=compound_id,
                priority=priority,
                state=TaskState.READY,
            )
    except IntegrityError:
        # Lost the race: another worker inserted the active task first.
        return RemoteTask.objects.filter(
            inchikey=inchikey,
            state__in=[s.value for s in TaskState.active_states()],
        ).first()


def claim_task(worker_id: str) -> RemoteTask | None:
    """Atomically claim the next due READY task with FOR UPDATE SKIP LOCKED.

    Must be called inside a transaction-managing caller; we open our own atomic
    block so the row lock is held only for the claim flip.
    """
    now = timezone.now()
    with transaction.atomic():
        task = (
            RemoteTask.objects.select_for_update(skip_locked=True)
            .filter(state=TaskState.READY)
            .filter(Q(next_run_at__isnull=True) | Q(next_run_at__lte=now))
            .order_by("-priority", "next_run_at", "id")
            .first()
        )
        if task is None:
            return None
        task.state = TaskState.LOCKED
        task.lock_owner = worker_id
        task.locked_at = now
        task.save(update_fields=["state", "lock_owner", "locked_at", "updated_at"])
        return task


def recover_stale_locks(lease_seconds: float) -> int:
    """Reset LOCKED/RUNNING tasks whose lock is older than the lease to READY.

    A worker that died mid-task must not strand work. Returns the number of rows
    recovered.
    """
    cutoff = timezone.now() - timedelta(seconds=lease_seconds)
    return (
        RemoteTask.objects.filter(
            state__in=[TaskState.LOCKED, TaskState.RUNNING, TaskState.ENTITY_LOOKUP],
            locked_at__lt=cutoff,
        ).update(
            state=TaskState.READY,
            lock_owner=None,
            locked_at=None,
            updated_at=timezone.now(),
        )
    )


def transition(task: RemoteTask, state: TaskState, **fields: object) -> None:
    """Persist a state transition plus any extra column updates."""
    task.state = state
    update_fields = ["state", "updated_at", *fields.keys()]
    for key, value in fields.items():
        setattr(task, key, value)
    task.save(update_fields=update_fields)


def schedule_retry(task: RemoteTask) -> None:
    """Increment ``attempt`` and park in RETRY_WAIT/ DEFERRED with backoff.

    When attempts are exhausted the task is parked as DEFERRED (still holding the
    dedup slot, retried ~daily) — NEVER discarded.
    """
    task.attempt += 1
    delay = backoff_seconds(task.attempt - 1, task.inchikey)
    next_run = timezone.now() + timedelta(seconds=delay)
    if task.attempt >= MAX_ATTEMPTS:
        transition(
            task,
            TaskState.DEFERRED,
            attempt=task.attempt,
            next_run_at=next_run,
            lock_owner=None,
            locked_at=None,
        )
    else:
        transition(
            task,
            TaskState.RETRY_WAIT,
            attempt=task.attempt,
            next_run_at=next_run,
            lock_owner=None,
            locked_at=None,
        )


def retry_after_override(task: RemoteTask, retry_after: float) -> None:
    """Park in RETRY_WAIT honouring a server Retry-After (absolute precedence)."""
    task.attempt += 1
    next_run = timezone.now() + timedelta(seconds=max(retry_after, 0))
    transition(
        task,
        TaskState.RETRY_WAIT,
        attempt=task.attempt,
        next_run_at=next_run,
        lock_owner=None,
        locked_at=None,
    )


def defer_task(task: RemoteTask, delay_seconds: float) -> None:
    """Park a task as DEFERRED (circuit open / quota) without counting a retry."""
    next_run = timezone.now() + timedelta(seconds=max(delay_seconds, 0))
    transition(
        task,
        TaskState.DEFERRED,
        next_run_at=next_run,
        lock_owner=None,
        locked_at=None,
    )


def reactivate_due_tasks() -> int:
    """Move RETRY_WAIT/DEFERRED tasks whose next_run_at elapsed back to READY."""
    now = timezone.now()
    return (
        RemoteTask.objects.filter(
            state__in=[TaskState.RETRY_WAIT, TaskState.DEFERRED, TaskState.POLL_WAIT],
            next_run_at__lte=now,
        ).update(state=TaskState.READY, updated_at=now)
    )


def finish_task(task: RemoteTask) -> None:
    """Mark a task DONE (success or permanent failure), freeing the dedup slot."""
    transition(task, TaskState.DONE, lock_owner=None, locked_at=None)
