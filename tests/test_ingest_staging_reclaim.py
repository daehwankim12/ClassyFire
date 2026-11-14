from __future__ import annotations

import pytest

from apps.batches.ingest import spool_text_input
from apps.batches.models import BatchItem, BatchJob, BatchQuotaEvent
from apps.batches.services import (
    IngestLeaseLost,
    claim_job_for_processing,
    stage_spooled_upload,
)
from apps.core.enums import BatchProcessingState


@pytest.mark.django_db(transaction=True)
def test_stale_staging_worker_cannot_commit_rows_or_quota(settings, tmp_path):
    """H-01: a stale worker reclaimed mid-staging must write NOTHING.

    Two workers claim a job that is in UPLOADING with a spooled artifact. The
    first (stale) worker is reclaimed by a second worker BEFORE it stages; when
    the stale worker tries to stage it must raise IngestLeaseLost and commit no
    rows / no quota event. The live worker then stages exactly once.
    """
    settings.INGEST_CLAIM_LEASE_SECONDS = 0
    settings.UPLOAD_ARTIFACT_DIR = str(tmp_path / "uploads")

    spooled = spool_text_input("CCO\nCCC\n")

    job = BatchJob.objects.create(
        secret_hash="hash",
        remote_enabled=False,
        remote_principal="staging-reclaim-test",
        processing_state=BatchProcessingState.UPLOADING,
        upload_artifact_path=spooled.relative_path,
        upload_original_name=spooled.original_name,
        upload_content_type=spooled.content_type,
        upload_size_bytes=spooled.size_bytes,
        upload_sha256=spooled.sha256,
    )

    # Worker A claims; its captured claim is now stale once worker B reclaims.
    stale = claim_job_for_processing("worker-a")
    assert stale is not None and stale.pk == job.pk

    # Worker B reclaims the same job (lease is 0s, so it is stealable).
    live = claim_job_for_processing("worker-b")
    assert live is not None and live.pk == job.pk

    # The stale worker must detect lease loss BEFORE writing any rows/quota.
    with pytest.raises(IngestLeaseLost):
        stage_spooled_upload(stale)

    assert BatchItem.objects.filter(job_id=job.pk).count() == 0
    # Only the in-request row_count=0 placeholder event exists is NOT created in
    # this test (the job was built directly), so a stale stage must add nothing.
    assert BatchQuotaEvent.objects.filter(row_count__gt=0).count() == 0

    # The live worker stages exactly once: rows created, one quota event,
    # state advanced to PARSING, no (job, row_order) collision.
    stage_spooled_upload(live)

    items = list(BatchItem.objects.filter(job_id=job.pk).order_by("row_order"))
    assert [item.row_order for item in items] == [0, 1]
    assert BatchQuotaEvent.objects.filter(row_count__gt=0).count() == 1
    quota_event = BatchQuotaEvent.objects.get(row_count__gt=0)
    assert quota_event.row_count == 2

    live.refresh_from_db()
    assert live.processing_state == BatchProcessingState.PARSING
    assert live.upload_artifact_path is None
    assert live.upload_size_bytes is None
    assert not (tmp_path / "uploads" / spooled.relative_path).exists()

    # Idempotent resume: re-entering the live worker stages nothing extra.
    stage_spooled_upload(live)
    assert BatchItem.objects.filter(job_id=job.pk).count() == 2
    assert BatchQuotaEvent.objects.filter(row_count__gt=0).count() == 1
