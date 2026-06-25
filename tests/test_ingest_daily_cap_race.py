from __future__ import annotations

import multiprocessing as mp

import pytest
from django.db import close_old_connections, connections
from django.db.models import Sum

from apps.batches.ingest import spool_text_input
from apps.batches.models import BatchItem, BatchJob, BatchQuotaEvent
from apps.batches.services import (
    claim_job_for_processing,
    process_claimed_job,
)
from apps.core.enums import BatchJobStatus, BatchProcessingState

PRINCIPAL = "daily-cap-race-test"


def _stage_one(worker_id: str, barrier: mp.Barrier, queue_out: mp.Queue) -> None:
    """Claim + stage ONE job for the shared principal, after a barrier.

    Mirrors the multi-process worker topology: process_jobs claims a job via
    SELECT...FOR UPDATE SKIP LOCKED, then stages the spooled artifact. The
    barrier forces both workers into the daily-cap critical section together so
    the per-principal serialization (or its absence) is exercised.
    """
    close_old_connections()
    barrier.wait()
    job = claim_job_for_processing(worker_id)
    if job is None:
        queue_out.put(("no-job", worker_id))
        return
    # process_claimed_job stages (UPLOADING->PARSING) and parks the job
    # terminal-FAILED with the daily-cap reason if the cap is exceeded.
    process_claimed_job(job)
    job.refresh_from_db()
    queue_out.put((job.processing_state, job.pk))


@pytest.mark.django_db(transaction=True)
def test_worker_daily_row_cap_is_race_safe(settings, tmp_path):
    """H-05: two same-principal workers must not BOTH exceed the daily row cap.

    Each worker stages a DIFFERENT spooled job for the SAME principal. Each job
    individually fits under JOB_CREATE_DAILY_ROW_CAP, but TOGETHER they exceed
    it. Pre-fix, both workers locked only their own job row, read the same
    used_rows, both passed the cap check, and both committed -> total accepted
    rows EXCEEDED the cap. With per-principal serialization, exactly one job is
    accepted (PARSING) and the other is parked terminal-FAILED.
    """
    settings.UPLOAD_ARTIFACT_DIR = str(tmp_path / "uploads")
    settings.JOB_CREATE_DAILY_ROW_CAP = 3
    settings.INGEST_CLAIM_LEASE_SECONDS = 300

    # Two jobs, each 2 rows: individually fit (2 <= 3), together exceed (4 > 3).
    for _ in range(2):
        spooled = spool_text_input("CCO\nCCC\n")
        BatchJob.objects.create(
            secret_hash="hash",
            remote_enabled=False,
            remote_principal=PRINCIPAL,
            processing_state=BatchProcessingState.UPLOADING,
            upload_artifact_path=spooled.relative_path,
            upload_original_name=spooled.original_name,
            upload_content_type=spooled.content_type,
            upload_size_bytes=spooled.size_bytes,
            upload_sha256=spooled.sha256,
        )

    barrier = mp.Barrier(2)
    queue_out: mp.Queue = mp.Queue()
    processes = [
        mp.Process(target=_stage_one, args=(f"worker-{i}", barrier, queue_out))
        for i in range(2)
    ]
    # Close the parent's DB connection BEFORE forking so each child opens its
    # own fresh socket to the (inherited) test DB.
    connections.close_all()
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    results = [queue_out.get(timeout=1) for _ in processes]
    states = [state for state, _ in results]

    # The cap is the load-bearing assertion: total accepted rows for this
    # principal must NOT exceed the cap. Pre-fix this is 4 (> 3); post-fix it is
    # at most 3 (exactly one 2-row job accepted).
    accepted_rows = (
        BatchQuotaEvent.objects.filter(
            kind=BatchQuotaEvent.KIND_JOB_ROWS,
            principal=PRINCIPAL,
        ).aggregate(total=Sum("row_count"))["total"]
        or 0
    )
    assert accepted_rows <= settings.JOB_CREATE_DAILY_ROW_CAP, (
        f"daily row cap exceeded: {accepted_rows} > {settings.JOB_CREATE_DAILY_ROW_CAP}"
    )

    # Exactly one job accepted (rows committed, advanced past UPLOADING), the
    # other parked terminal-FAILED. The accepted job's downstream state varies
    # (it runs the full pipeline), so assert on FAILED-vs-not, not on PARSING.
    failed_states = states.count(BatchProcessingState.FAILED)
    assert failed_states == 1, (
        f"expected exactly one parked-FAILED job, got states={states}"
    )

    # The accepted principal has exactly one 2-row job's BatchItems (the rejected
    # job's rows were rolled back).
    assert BatchItem.objects.filter(job__remote_principal=PRINCIPAL).count() == 2
    # Exactly one quota event recorded (one accepted job).
    assert (
        BatchQuotaEvent.objects.filter(
            kind=BatchQuotaEvent.KIND_JOB_ROWS, principal=PRINCIPAL
        ).count()
        == 1
    )
    # The rejected job is terminal-FAILED with the daily-cap reason.
    failed_job = BatchJob.objects.get(
        remote_principal=PRINCIPAL, status=BatchJobStatus.FAILED
    )
    assert "Daily row quota exceeded" in (failed_job.processing_error or "")
