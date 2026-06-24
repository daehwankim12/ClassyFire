"""Fan-out: apply one remote result to ALL batch items waiting on an InChIKey.

Global dedupe means a single ``RemoteTask`` is processed once per InChIKey, then
its result fans out to every ``BatchItem`` (across jobs) that was waiting on that
key. After flipping item statuses we recompute the affected ``BatchJob``
counters via the canonical ``recompute_job_counters`` from
``apps/batches/services``.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable

from django.db import transaction

from apps.batches.models import BatchItem
from apps.core.enums import BatchItemStatus

# BatchItem statuses considered "waiting on a remote result" for a given key.
WAITING_STATUSES = (
    BatchItemStatus.REMOTE_DELAYED,
    BatchItemStatus.PENDING_CLASSIFICATION,
)

FOUND_STATUSES = (
    BatchItemStatus.FOUND_LIVE_CACHE,
    BatchItemStatus.FOUND_SEED_CACHE,
    BatchItemStatus.FOUND_WISHART_ENTITY,
    BatchItemStatus.COMPLETE_AFTER_WISHART,
)


def mark_items_delayed(inchikey: str) -> list[uuid.UUID]:
    """Transition all PENDING_CLASSIFICATION items for *inchikey* to REMOTE_DELAYED.

    Called by the worker after parking a task for retry/defer (schedule_retry or
    the circuit-open defer path) so that the user-facing REMOTE_DELAYED status —
    spec §6.2: "Wishart 장애로 자동 재시도 대기" — is immediately visible rather
    than staying as PENDING_CLASSIFICATION until the task eventually resolves.

    Returns the list of affected BatchJob ids.  Only PENDING_CLASSIFICATION items
    are transitioned; items already in REMOTE_DELAYED are not touched.
    """
    from apps.batches.services import recompute_job_counters

    with transaction.atomic():
        affected_jobs: list[uuid.UUID] = list(
            BatchItem.objects.filter(
                inchikey=inchikey,
                status=BatchItemStatus.PENDING_CLASSIFICATION,
            )
            .values_list("job_id", flat=True)
            .distinct()
        )
        if affected_jobs:
            BatchItem.objects.filter(
                inchikey=inchikey,
                status=BatchItemStatus.PENDING_CLASSIFICATION,
            ).update(status=BatchItemStatus.REMOTE_DELAYED)
            for job_id in affected_jobs:
                recompute_job_counters(job_id)
    return affected_jobs


def fan_out_result(inchikey: str, status: BatchItemStatus) -> list[uuid.UUID]:
    """Set all waiting items for ``inchikey`` to ``status``; recompute counters.

    Returns the list of affected BatchJob ids.
    """
    # Import here to avoid circular imports at module load time.
    from apps.batches.services import recompute_job_counters
    from apps.classifications.services import snapshot_current_classification

    snapshot = (
        snapshot_current_classification(inchikey)
        if status in FOUND_STATUSES
        else None
    )

    with transaction.atomic():
        affected_jobs: list[uuid.UUID] = list(
            BatchItem.objects.filter(
                inchikey=inchikey, status__in=WAITING_STATUSES
            )
            .values_list("job_id", flat=True)
            .distinct()
        )
        update_values: dict[str, object] = {"status": status}
        if snapshot is not None:
            update_values["classification_snapshot"] = snapshot
        elif status not in FOUND_STATUSES:
            update_values["classification_snapshot"] = None

        BatchItem.objects.filter(
            inchikey=inchikey, status__in=WAITING_STATUSES
        ).update(**update_values)
        for job_id in affected_jobs:
            recompute_job_counters(job_id)
    return affected_jobs


def recompute_job_counters(job_ids: Iterable[uuid.UUID]) -> None:
    """Recompute denormalised BatchJob counters from item statuses.

    Thin wrapper kept for backwards-compatibility: delegates to the canonical
    implementation in ``apps/batches/services.recompute_job_counters``.
    """
    from apps.batches.services import recompute_job_counters as _recompute

    for job_id in job_ids:
        _recompute(job_id)
