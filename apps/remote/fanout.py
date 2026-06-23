"""Fan-out: apply one remote result to ALL batch items waiting on an InChIKey.

Global dedupe means a single ``RemoteTask`` is processed once per InChIKey, then
its result fans out to every ``BatchItem`` (across jobs) that was waiting on that
key. After flipping item statuses we recompute the affected ``BatchJob``
counters *here* (grouped query per job) so we never have to edit
``apps/batches/services.py``.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from apps.batches.models import BatchItem, BatchJob
from apps.core.enums import BatchItemStatus, BatchJobStatus

# BatchItem statuses considered "waiting on a remote result" for a given key.
WAITING_STATUSES = (
    BatchItemStatus.REMOTE_DELAYED,
    BatchItemStatus.PENDING_CLASSIFICATION,
)

# Item statuses that count toward each BatchJob counter.
_TERMINAL_COMPLETE = {
    BatchItemStatus.FOUND_LIVE_CACHE,
    BatchItemStatus.FOUND_SEED_CACHE,
    BatchItemStatus.FOUND_WISHART_ENTITY,
    BatchItemStatus.COMPLETE_AFTER_WISHART,
}


def fan_out_result(inchikey: str, status: BatchItemStatus) -> list[uuid.UUID]:
    """Set all waiting items for ``inchikey`` to ``status``; recompute counters.

    Returns the list of affected BatchJob ids.
    """
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
        recompute_job_counters(affected_jobs)
    return affected_jobs


def recompute_job_counters(job_ids: Iterable[uuid.UUID]) -> None:
    """Recompute denormalised BatchJob counters from item statuses.

    Done inside ``apps/remote`` (grouped per job) to avoid editing
    ``apps/batches/services.py``.
    """
    for job_id in job_ids:
        counts = dict(
            BatchItem.objects.filter(job_id=job_id)
            .values_list("status")
            .annotate(n=Count("id"))
        )
        total = sum(counts.values())
        completed = sum(counts.get(s, 0) for s in _TERMINAL_COMPLETE)
        completed += counts.get(BatchItemStatus.NEED_STRUCTURE, 0)
        completed += counts.get(BatchItemStatus.INVALID_INPUT, 0)
        completed += counts.get(BatchItemStatus.RESULT_KEY_MISMATCH, 0)
        remote_pending = counts.get(
            BatchItemStatus.REMOTE_DELAYED, 0
        ) + counts.get(BatchItemStatus.PENDING_CLASSIFICATION, 0)

        job = BatchJob.objects.filter(id=job_id).first()
        if job is None:
            continue
        job.total_count = total
        job.completed_count = completed
        job.local_hit_count = counts.get(BatchItemStatus.FOUND_LIVE_CACHE, 0)
        job.seed_hit_count = counts.get(BatchItemStatus.FOUND_SEED_CACHE, 0)
        job.remote_pending_count = remote_pending
        job.need_structure_count = counts.get(BatchItemStatus.NEED_STRUCTURE, 0)
        job.invalid_count = counts.get(BatchItemStatus.INVALID_INPUT, 0)

        if total > 0 and remote_pending == 0 and completed >= total:
            if job.status != BatchJobStatus.COMPLETE:
                job.status = BatchJobStatus.COMPLETE
                job.completed_at = timezone.now()
        job.save()
