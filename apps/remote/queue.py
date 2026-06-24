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
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.core.enums import TaskState
from apps.remote.budget import normalize_principal, reserve_remote_budget
from apps.remote.models import RemotePrincipalState, RemoteTask


class LeaseLost(Exception):  # noqa: N818 - domain name fixed by the F-07 contract
    """Raised when a conditional transition matches 0 rows (F-07).

    The task's lease was stolen or reclaimed (its ``lease_version``/
    ``lease_token`` no longer match), so this worker MUST abort processing it and
    must NOT overwrite the new owner's state.
    """

    def __init__(self, task_pk: int) -> None:
        super().__init__(
            f"lease lost for remote_task pk={task_pk}: version/token no longer match"
        )
        self.task_pk = task_pk

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
MIN_AWARE_DATETIME = datetime.min.replace(tzinfo=UTC)


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
    inchikey: str,
    *,
    compound_id: int | None = None,
    priority: int = 0,
    principal: str | None = None,
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
            normalized_principal = normalize_principal(principal)
            task = RemoteTask.objects.create(
                inchikey=inchikey,
                compound_id=compound_id,
                priority=priority,
                principal=normalized_principal,
                state=TaskState.READY,
            )
            reserve_remote_budget(normalized_principal, task=task)
            return task
    except IntegrityError:
        # Lost the race: another worker inserted the active task first.
        return RemoteTask.objects.filter(
            inchikey=inchikey,
            state__in=[s.value for s in TaskState.active_states()],
        ).first()


def _due_ready_tasks(now):
    return (
        RemoteTask.objects.filter(state=TaskState.READY)
        .filter(Q(next_run_at__isnull=True) | Q(next_run_at__lte=now))
    )


def _locked_principal_states(principals: list[str]) -> dict[str, RemotePrincipalState]:
    for principal in principals:
        RemotePrincipalState.objects.get_or_create(
            principal=principal,
            defaults={"last_claimed_at": None, "claim_count": 0},
        )
    return {
        state.principal: state
        for state in RemotePrincipalState.objects.select_for_update()
        .filter(principal__in=principals)
        .order_by("principal")
    }


def _claim_task_once(
    worker_id: str,
    *,
    exclude_ids: set[int] | None,
    structure_required: bool,
    now,
) -> tuple[RemoteTask | None, bool]:
    base_qs = _due_ready_tasks(now)
    if exclude_ids:
        base_qs = base_qs.exclude(id__in=exclude_ids)
    if structure_required:
        base_qs = base_qs.filter(
            Q(compound__canonical_smiles__isnull=False)
            | Q(compound__inchi__isnull=False)
        )

    top_priority = base_qs.order_by("-priority").values_list(
        "priority", flat=True
    ).first()
    if top_priority is None:
        return None, False
    principals = list(
        base_qs.filter(priority=top_priority)
        .order_by("principal")
        .values_list("principal", flat=True)
        .distinct()
    )
    states = _locked_principal_states(principals)

    def principal_fairness_key(principal: str) -> tuple[bool, object, str]:
        state = states.get(principal)
        last_claimed_at = state.last_claimed_at if state is not None else None
        return (
            last_claimed_at is not None,
            last_claimed_at or MIN_AWARE_DATETIME,
            principal,
        )

    task = None
    for principal in sorted(principals, key=principal_fairness_key):
        candidate_qs = (
            base_qs.select_for_update(skip_locked=True)
            .filter(priority=top_priority, principal=principal)
            .order_by("next_run_at", "id")
        )
        task = cast("RemoteTask | None", candidate_qs.first())
        if task is not None:
            break
    if task is None:
        return None, True

    # Issue a fresh fencing token + bump the monotonic lease version (F-07)
    # so any previous holder's late writes are rejected.
    task.state = TaskState.LOCKED
    task.lock_owner = worker_id
    task.locked_at = now
    task.lease_token = uuid.uuid4()
    task.lease_version = task.lease_version + 1
    task.save(
        update_fields=[
            "state",
            "lock_owner",
            "locked_at",
            "lease_token",
            "lease_version",
            "updated_at",
        ]
    )
    RemotePrincipalState.objects.filter(pk=states[task.principal].pk).update(
        last_claimed_at=now,
        claim_count=F("claim_count") + 1,
    )
    return task, False


def claim_task(
    worker_id: str,
    *,
    exclude_ids: set[int] | None = None,
    structure_required: bool = False,
) -> RemoteTask | None:
    """Atomically claim the next due READY task with FOR UPDATE SKIP LOCKED.

    Must be called inside a transaction-managing caller; we open our own atomic
    block so the row lock is held only for the claim flip.
    """
    for _attempt in range(3):
        now = timezone.now()
        with transaction.atomic():
            task, retry = _claim_task_once(
                worker_id,
                exclude_ids=exclude_ids,
                structure_required=structure_required,
                now=now,
            )
        if task is not None or not retry:
            return task
    return None


def recover_stale_locks(lease_seconds: float) -> int:
    """Reset LOCKED/RUNNING tasks whose lock is older than the lease to READY.

    A worker that died mid-task must not strand work. Returns the number of rows
    recovered.
    """
    cutoff = timezone.now() - timedelta(seconds=lease_seconds)
    # Bump lease_version + clear the token so the dead holder's late, fenced
    # writes can no longer match (F-07): the reclaim invalidates its lease.
    return (
        RemoteTask.objects.filter(
            state__in=[TaskState.LOCKED, TaskState.RUNNING, TaskState.ENTITY_LOOKUP],
            locked_at__lt=cutoff,
        ).update(
            state=TaskState.READY,
            lock_owner=None,
            locked_at=None,
            lease_token=None,
            lease_version=F("lease_version") + 1,
            updated_at=timezone.now(),
        )
    )


def transition(task: RemoteTask, state: TaskState, **fields: object) -> None:
    """Persist a lease-fenced state transition plus any extra column updates.

    EVERY transition is conditional on the task's current
    ``(pk, lease_version, lease_token)`` (F-07): the UPDATE only writes the row
    if the lease still matches. If 0 rows are updated the lease was stolen /
    reclaimed by another worker — we raise :class:`LeaseLost` and do NOT
    overwrite the new owner's state. The in-memory ``task`` is mutated to reflect
    the intended new values only after the conditional UPDATE succeeds.

    Tasks that have never been claimed (``lease_token is None``) are transitioned
    on the ``pk + lease_version`` pair alone — there is no competing owner to
    fence against until a claim issues a token.
    """
    update_values: dict[str, object] = {"state": state, "updated_at": timezone.now()}
    update_values.update(fields)

    guard = RemoteTask.objects.filter(pk=task.pk, lease_version=task.lease_version)
    if task.lease_token is not None:
        guard = guard.filter(lease_token=task.lease_token)
    updated = guard.update(**update_values)
    if updated == 0:
        raise LeaseLost(task.pk)

    task.state = state
    for key, value in fields.items():
        setattr(task, key, value)


def lock_owned_task(task: RemoteTask) -> RemoteTask:
    """Row-lock ``task`` and assert this worker still owns its lease (F-07).

    ``SELECT ... FOR UPDATE`` the row, then verify its
    ``(lease_version, lease_token, lock_owner)`` still match the in-memory
    ``task`` the caller holds. If the lease was stolen/reclaimed (a fresh worker
    claimed it after stale-lock recovery) the triple no longer matches and we
    raise :class:`LeaseLost` BEFORE the caller commits any side effect.

    MUST be called inside a ``transaction.atomic()`` so the returned row stays
    locked for the duration of the fenced work; the lock + ownership check make a
    subsequent :func:`transition_locked` on the returned instance race-free.
    """
    locked = RemoteTask.objects.select_for_update().get(pk=task.pk)
    if (
        locked.lease_version != task.lease_version
        or locked.lease_token != task.lease_token
        or locked.lock_owner != task.lock_owner
    ):
        raise LeaseLost(task.pk)
    return locked


def transition_locked(
    locked: RemoteTask, state: TaskState, **fields: object
) -> None:
    """Transition an ALREADY-LOCKED, ownership-verified row (no second guard).

    Unlike :func:`transition`, this does NOT re-run the lease-conditional UPDATE:
    the caller has already row-locked ``locked`` and verified ownership via
    :func:`lock_owned_task` within the SAME ``transaction.atomic()``, so no other
    worker can mutate the row until this transaction commits. A plain
    ``UPDATE ... WHERE pk=...`` is therefore correct. The in-memory ``locked`` is
    mutated to match the written values.

    PRECONDITION: ``locked`` was returned by :func:`lock_owned_task` in the
    enclosing atomic block (row-locked + lease verified).
    """
    update_values: dict[str, object] = {"state": state, "updated_at": timezone.now()}
    update_values.update(fields)
    RemoteTask.objects.filter(pk=locked.pk).update(**update_values)
    locked.state = state
    for key, value in fields.items():
        setattr(locked, key, value)


def claim_extra(task: RemoteTask, worker_id: str) -> None:
    """Claim a select_for_update'd extra into LOCKED with a fresh lease (F-07).

    Mirrors :func:`claim_task`'s fencing for tasks folded into a submit batch:
    issues a new ``lease_token`` and bumps ``lease_version`` so subsequent
    fenced transitions of the extra are owned by this worker. Caller MUST already
    hold the row lock (``SELECT ... FOR UPDATE SKIP LOCKED``).
    """
    now = timezone.now()
    token = uuid.uuid4()
    RemoteTask.objects.filter(pk=task.pk).update(
        state=TaskState.LOCKED,
        lock_owner=worker_id,
        locked_at=now,
        lease_token=token,
        lease_version=F("lease_version") + 1,
        updated_at=now,
    )
    task.refresh_from_db(
        fields=["state", "lock_owner", "locked_at", "lease_token", "lease_version"]
    )


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
    # Clear any stale fencing token + bump the version so a previous holder of a
    # parked POLL_WAIT task cannot match after the task is reactivated (F-07).
    return (
        RemoteTask.objects.filter(
            state__in=[TaskState.RETRY_WAIT, TaskState.DEFERRED, TaskState.POLL_WAIT],
            next_run_at__lte=now,
        ).update(
            state=TaskState.READY,
            lease_token=None,
            lease_version=F("lease_version") + 1,
            updated_at=now,
        )
    )


def finish_task(task: RemoteTask) -> None:
    """Mark a task DONE (success or permanent failure), freeing the dedup slot."""
    transition(task, TaskState.DONE, lock_owner=None, locked_at=None)
