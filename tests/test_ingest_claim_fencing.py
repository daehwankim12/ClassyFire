import pytest

from apps.batches.models import BatchJob
from apps.batches.services import (
    IngestLeaseLost,
    claim_job_for_processing,
    process_claimed_job,
)
from apps.core.enums import BatchProcessingState


@pytest.mark.django_db(transaction=True)
def test_active_ingest_claim_is_not_reclaimed_before_stale(settings):
    settings.INGEST_CLAIM_LEASE_SECONDS = 300
    job = BatchJob.objects.create(
        secret_hash="hash",
        processing_state=BatchProcessingState.PARSING,
    )

    first = claim_job_for_processing("worker-a")
    assert first.pk == job.pk

    second = claim_job_for_processing("worker-b")
    assert second is None


@pytest.mark.django_db(transaction=True)
def test_stale_ingest_worker_cannot_checkpoint_after_reclaim(settings):
    settings.INGEST_CLAIM_LEASE_SECONDS = 0
    BatchJob.objects.create(
        secret_hash="hash",
        processing_state=BatchProcessingState.PARSING,
    )
    first = claim_job_for_processing("worker-a")
    reclaimed = claim_job_for_processing("worker-b")

    assert first.pk == reclaimed.pk
    with pytest.raises(IngestLeaseLost):
        process_claimed_job(first)
