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


def fan_out_result(inchikey: str, status: BatchItemStatus) -> list[uuid.UUID]:
    """Set all waiting items for ``inchikey`` to ``status``; recompute counters.

    Returns the list of affected BatchJob ids.
    """
    # Import here to avoid circular imports at module load time.
    from apps.batches.services import recompute_job_counters

    with transaction.atomic():
        affected_jobs: list[uuid.UUID] = list(
            BatchItem.objects.filter(
                inchikey=inchikey, status__in=WAITING_STATUSES
            )
            .values_list("job_id", flat=True)
            .distinct()
        )
        BatchItem.objects.filter(
            inchikey=inchikey, status__in=WAITING_STATUSES
        ).update(status=status)
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
