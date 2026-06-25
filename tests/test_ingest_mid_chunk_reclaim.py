from __future__ import annotations

import pytest

from apps.batches.models import BatchItem, BatchJob
from apps.batches.services import (
    IngestLeaseLost,
    claim_job_for_processing,
    process_claimed_job,
)
from apps.compounds.models import CompoundUsage
from apps.core.enums import BatchItemStatus, BatchProcessingState
from apps.remote.models import RemoteBudgetLedger, RemoteTask

INCHIKEY = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"


@pytest.mark.django_db(transaction=True)
def test_stale_mid_chunk_worker_cannot_commit_ingest_side_effects(
    monkeypatch, settings
):
    settings.INGEST_CLAIM_LEASE_SECONDS = 0
    settings.REMOTE_DAILY_BUDGET_PER_PRINCIPAL = 100

    job = BatchJob.objects.create(
        secret_hash="hash",
        remote_enabled=True,
        remote_principal="mid-chunk-test",
        processing_state=BatchProcessingState.PARSING,
    )
    BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input="CCO",
        status=BatchItemStatus.PENDING_NORMALIZATION,
    )

    stale = claim_job_for_processing("worker-a")
    assert stale is not None and stale.pk == job.pk

    reclaimed_jobs: list[BatchJob] = []

    import apps.batches.services as services

    real_lookup = services.lookup_current_classifications

    def reclaim_before_commit(inchikeys):
        if not reclaimed_jobs:
            reclaimed = claim_job_for_processing("worker-b")
            assert reclaimed is not None and reclaimed.pk == job.pk
            reclaimed_jobs.append(reclaimed)
        return real_lookup(inchikeys)

    monkeypatch.setattr(
        services, "lookup_current_classifications", reclaim_before_commit
    )

    with pytest.raises(IngestLeaseLost):
        process_claimed_job(stale)

    item = BatchItem.objects.get(job=job, row_order=0)
    assert item.status == BatchItemStatus.PENDING_NORMALIZATION
    assert CompoundUsage.objects.filter(inchikey=INCHIKEY).count() == 0
    assert RemoteTask.objects.filter(inchikey=INCHIKEY).count() == 0
    assert RemoteBudgetLedger.objects.filter(task__inchikey=INCHIKEY).count() == 0

    assert len(reclaimed_jobs) == 1
    process_claimed_job(reclaimed_jobs[0])

    item.refresh_from_db()
    assert item.status == BatchItemStatus.PENDING_CLASSIFICATION
    assert CompoundUsage.objects.get(inchikey=INCHIKEY).seen_count == 1
    assert RemoteTask.objects.filter(inchikey=INCHIKEY).count() == 1
    assert RemoteBudgetLedger.objects.filter(task__inchikey=INCHIKEY).count() == 1
