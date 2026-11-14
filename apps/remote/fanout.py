"""Fan-out: apply one remote result to its task generation's waiting items.

Each ``BatchItem`` records the exact ``RemoteTask`` generation it joined. Results
are scoped to that identity, so a terminal generation cannot consume an item
that concurrently joined a newer task for the same InChIKey. Job counters are
recomputed only after the task transaction commits and releases its row locks.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable

from django.db import transaction
from django.db.models import Q

from apps.batches.models import BatchItem
from apps.batches.remote_delay import (
    AUTH_BLOCKED_MESSAGE,
    PROTOCOL_RETRY_MESSAGE,
    RETRYABLE_REMOTE_MESSAGE,
)
from apps.core.enums import BatchItemStatus
from apps.remote.models import RemoteTask

# BatchItem statuses considered "waiting on a remote result" for a given key.
WAITING_STATUSES = (
    BatchItemStatus.REMOTE_DELAYED,
    BatchItemStatus.PENDING_CLASSIFICATION,
)

FOUND_STATUSES = (
    BatchItemStatus.FOUND_LIVE_CACHE,
    BatchItemStatus.FOUND_WISHART_ENTITY,
    BatchItemStatus.COMPLETE_AFTER_WISHART,
)


def _task_item_filter(task: RemoteTask) -> Q:
    # Null membership is retained only for pre-migration rows and items parked
    # before queue admission. Adopt those rows into the currently active
    # generation while never crossing into another recorded generation.
    return Q(remote_task_id=task.pk) | Q(
        remote_task__isnull=True,
        inchikey=task.inchikey,
    )


def lock_task_items(task_id: int) -> None:
    """Lock a generation's waiting items in deterministic item -> task order."""
    task = RemoteTask.objects.filter(pk=task_id).only("pk", "inchikey").first()
    if task is None:
        return
    list(
        BatchItem.objects.select_for_update()
        .filter(_task_item_filter(task), status__in=WAITING_STATUSES)
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def _recompute_jobs_after_commit(job_ids: Iterable[uuid.UUID]) -> None:
    """Defer BatchJob locks until the enclosing task transaction has committed."""
    ids = tuple(dict.fromkeys(job_ids))
    if not ids:
        return

    def recompute_one(job_id: uuid.UUID) -> None:
        from apps.batches.services import recompute_job_counters

        recompute_job_counters(job_id)

    # One robust callback per job: a failure (lock wait, deadlock, crash
    # window) on one job's counters is logged by Django and cannot skip the
    # others or escape into the caller after its transaction has committed.
    # Counters left stale this way are repaired by the next recompute for the
    # job (ingest, any later fan-out, or the admin "recompute counters").
    for job_id in ids:
        transaction.on_commit(
            lambda job_id=job_id: recompute_one(job_id), robust=True
        )


def mark_items_delayed(
    task: RemoteTask, *, message: str | None = None
) -> list[uuid.UUID]:
    """Transition this generation's PENDING_CLASSIFICATION items to REMOTE_DELAYED.

    Called by the worker after parking a task for retry/defer so that the
    user-facing REMOTE_DELAYED status —
    spec §6.2: "Wishart 장애로 자동 재시도 대기" — is immediately visible rather
    than staying as PENDING_CLASSIFICATION until the task eventually resolves.

    Returns the list of affected BatchJob ids.  Only PENDING_CLASSIFICATION items
    are transitioned; items already in REMOTE_DELAYED are not touched.
    """
    with transaction.atomic():
        update_values: dict[str, object] = {
            "status": BatchItemStatus.REMOTE_DELAYED,
            "remote_task_id": task.pk,
        }
        delayed_message = message or _delay_message_for(task)
        if delayed_message:
            update_values["message"] = delayed_message
        updated = BatchItem.objects.filter(
            _task_item_filter(task),
            status=BatchItemStatus.PENDING_CLASSIFICATION,
        ).update(**update_values)
        affected_jobs: list[uuid.UUID] = []
        if updated:
            affected_jobs = list(
                BatchItem.objects.filter(
                    remote_task_id=task.pk,
                    status=BatchItemStatus.REMOTE_DELAYED,
                )
                .values_list("job_id", flat=True)
                .distinct()
            )
            _recompute_jobs_after_commit(affected_jobs)
    return affected_jobs


def _delay_message_for(task: RemoteTask) -> str | None:
    from apps.core.enums import ProviderControlState, TaskState
    from apps.remote.models import (
        RemoteAttempt,
        RemoteProviderControl,
        RemoteTask,
    )

    latest_attempt = (
        RemoteAttempt.objects.filter(task_id=task.pk)
        .order_by("-created_at", "-pk")
        .first()
    )
    if RemoteProviderControl.objects.filter(
        provider="wishart",
        state=ProviderControlState.AUTH_BLOCKED,
    ).exists() or (
        latest_attempt is not None
        and latest_attempt.classification == "AUTH_FAILURE"
    ):
        return AUTH_BLOCKED_MESSAGE
    if latest_attempt is not None:
        if latest_attempt.classification == "RETRYABLE":
            return RETRYABLE_REMOTE_MESSAGE
        if latest_attempt.http_status in {408, 425, 429}:
            return RETRYABLE_REMOTE_MESSAGE
        if latest_attempt.http_status is not None and latest_attempt.http_status >= 500:
            return RETRYABLE_REMOTE_MESSAGE
        if latest_attempt.classification == "PROTOCOL_ERROR":
            return PROTOCOL_RETRY_MESSAGE

    task_state = (
        RemoteTask.objects.filter(pk=task.pk)
        .values_list("state", flat=True)
        .first()
    )
    if task_state == TaskState.RETRY_WAIT:
        return RETRYABLE_REMOTE_MESSAGE
    return None


def fan_out_result(task: RemoteTask, status: BatchItemStatus) -> list[uuid.UUID]:
    """Set this task generation's waiting items to ``status`` and update counters.

    Returns the list of affected BatchJob ids.
    """
    from apps.classifications.services import snapshot_current_classification

    snapshot = (
        snapshot_current_classification(task.inchikey)
        if status in FOUND_STATUSES
        else None
    )

    with transaction.atomic():
        update_values: dict[str, object] = {
            "status": status,
            "remote_task_id": task.pk,
        }
        if snapshot is not None:
            update_values["classification_snapshot"] = snapshot
        elif status not in FOUND_STATUSES:
            update_values["classification_snapshot"] = None

        updated = BatchItem.objects.filter(
            _task_item_filter(task), status__in=WAITING_STATUSES
        ).update(**update_values)
        affected_jobs: list[uuid.UUID] = []
        if updated:
            affected_jobs = list(
                BatchItem.objects.filter(
                    remote_task_id=task.pk,
                    status=status,
                )
                .values_list("job_id", flat=True)
                .distinct()
            )
            _recompute_jobs_after_commit(affected_jobs)
    return affected_jobs


def recompute_job_counters(job_ids: Iterable[uuid.UUID]) -> None:
    """Recompute denormalised BatchJob counters from item statuses.

    Thin wrapper kept for backwards-compatibility: delegates to the canonical
    implementation in ``apps/batches/services.recompute_job_counters``.
    """
    from apps.batches.services import recompute_job_counters as _recompute

    for job_id in job_ids:
        _recompute(job_id)
