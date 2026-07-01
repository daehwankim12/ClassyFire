"""Tests for the stuck-remote-task timeout reaper (queue.timeout_stale_tasks).

The remote queue never discards a task on its own — an exhausted task parks as
DEFERRED and retries ~daily forever — so a permanently-unresolvable InChIKey
keeps its BatchItem in REMOTE_DELAYED and pins the job to IN_PROGRESS. The reaper
gives up after REMOTE_DELAYED_TIMEOUT_HOURS, marking the task DONE and fanning
its items out to REMOTE_TIMED_OUT so the job can complete.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.batches.models import BatchItem, BatchJob
from apps.batches.services import recompute_job_counters
from apps.core.enums import (
    BatchItemStatus,
    BatchJobStatus,
    BatchProcessingState,
    TaskState,
)
from apps.remote import queue
from apps.remote.models import RemoteTask

pytestmark = pytest.mark.django_db(transaction=True)

IK_A = "RDHQFKQIGNGIED-MRVPVSSYSA-N"
IK_B = "WQZGKKKJIJFFOK-GASJEMHNSA-N"


def _job() -> BatchJob:
    return BatchJob.objects.create(secret_hash="x", total_count=1)


def _delayed_item(job: BatchJob, inchikey: str, row: int = 0) -> BatchItem:
    return BatchItem.objects.create(
        job=job,
        row_order=row,
        original_input=inchikey,
        inchikey=inchikey,
        status=BatchItemStatus.REMOTE_DELAYED,
    )


def _stuck_task(inchikey: str, *, age_hours: float, attempt: int = 7) -> RemoteTask:
    """An exhausted DEFERRED task whose created_at is ``age_hours`` in the past."""
    task = RemoteTask.objects.create(
        inchikey=inchikey, state=TaskState.DEFERRED, attempt=attempt
    )
    # created_at is auto_now_add; force it into the past with a bare UPDATE.
    RemoteTask.objects.filter(pk=task.pk).update(
        created_at=timezone.now() - dt.timedelta(hours=age_hours)
    )
    return task


def test_timeout_resolves_stuck_task_and_completes_job():
    job = _job()
    _delayed_item(job, IK_A)
    task = _stuck_task(IK_A, age_hours=30)
    recompute_job_counters(job.pk)
    job.refresh_from_db()
    assert job.status == BatchJobStatus.IN_PROGRESS  # blocked by the delayed item

    timed_out = queue.timeout_stale_tasks(timeout_hours=24)
    assert timed_out == 1

    task.refresh_from_db()
    assert task.state == TaskState.DONE

    item = BatchItem.objects.get(job=job, inchikey=IK_A)
    assert item.status == BatchItemStatus.REMOTE_TIMED_OUT
    assert "timed out" in (item.message or "").lower()

    job.refresh_from_db()
    assert job.status == BatchJobStatus.COMPLETE
    assert job.processing_state == BatchProcessingState.COMPLETE
    # Timed-out items are terminal failures, not successful classifications: they
    # count toward "무효"(invalid), never "완료"(completed).
    assert job.completed_count == 0
    assert job.invalid_count == 1


def test_timeout_leaves_fresh_task_untouched():
    job = _job()
    _delayed_item(job, IK_B)
    task = _stuck_task(IK_B, age_hours=1)  # young: well under the 24h horizon

    assert queue.timeout_stale_tasks(timeout_hours=24) == 0

    task.refresh_from_db()
    assert task.state == TaskState.DEFERRED
    item = BatchItem.objects.get(job=job, inchikey=IK_B)
    assert item.status == BatchItemStatus.REMOTE_DELAYED


def test_timeout_disabled_when_hours_non_positive():
    job = _job()
    _delayed_item(job, IK_A)
    _stuck_task(IK_A, age_hours=100)

    assert queue.timeout_stale_tasks(timeout_hours=0) == 0
    item = BatchItem.objects.get(job=job, inchikey=IK_A)
    assert item.status == BatchItemStatus.REMOTE_DELAYED


def test_recompute_counts_timed_out_as_invalid_but_job_completes():
    """A job whose only item is REMOTE_TIMED_OUT is terminal (COMPLETE), and the
    item counts as a failure (invalid), NOT a successful classification."""
    job = _job()
    BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=IK_A,
        inchikey=IK_A,
        status=BatchItemStatus.REMOTE_TIMED_OUT,
    )
    recompute_job_counters(job.pk)
    job.refresh_from_db()
    assert job.status == BatchJobStatus.COMPLETE
    assert job.completed_count == 0
    assert job.invalid_count == 1


def test_timeout_terminalizes_live_query_submission_state():
    """Timed-out submitted tasks must not leave live rows that poison resubmit."""
    from apps.core.enums import QueryState, RemoteQueryItemApplyState, SubmissionState
    from apps.remote.models import (
        RemoteQuery,
        RemoteQueryItem,
        RemoteSubmission,
        RemoteSubmissionMember,
    )

    job = _job()
    _delayed_item(job, IK_A)
    task = RemoteTask.objects.create(inchikey=IK_A, state=TaskState.POLL_WAIT)
    RemoteTask.objects.filter(pk=task.pk).update(
        created_at=timezone.now() - dt.timedelta(hours=30)
    )
    task.refresh_from_db()
    query = RemoteQuery.objects.create(
        provider="wishart",
        remote_query_id="remote-timeout-test",
        payload_hash="payload-hash",
        state=QueryState.SUBMITTED,
    )
    RemoteQueryItem.objects.create(query=query, task=task, inchikey=IK_A)
    submission = RemoteSubmission.objects.create(
        provider="wishart",
        payload_hash="payload-hash",
        state=SubmissionState.SUBMITTED,
        query=query,
        remote_query_id=query.remote_query_id,
        request_payload={"structures": ["C"]},
    )
    member = RemoteSubmissionMember.objects.create(
        submission=submission,
        provider="wishart",
        inchikey=IK_A,
        structure_hash="0" * 64,
    )

    assert queue.timeout_stale_tasks(timeout_hours=24) == 1

    task.refresh_from_db()
    query.refresh_from_db()
    submission.refresh_from_db()
    member.refresh_from_db()
    qitem = RemoteQueryItem.objects.get(query=query, inchikey=IK_A)

    assert task.state == TaskState.DONE
    assert qitem.apply_state == RemoteQueryItemApplyState.FAILED
    assert "timed out" in (qitem.apply_detail or "").lower()
    assert query.state == QueryState.FAILED
    assert query.completed_at is not None
    assert submission.state == SubmissionState.FAILED
    assert "timed out" in (submission.detail or "").lower()
    assert member.active is False
    assert member.release_reason == "task_timed_out"
