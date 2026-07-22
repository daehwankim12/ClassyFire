from __future__ import annotations

import multiprocessing as mp

import pytest
from django.db import close_old_connections, connections

from apps.core.enums import TaskState
from apps.remote.models import RemoteTask
from apps.remote.queue import RemoteQueueBackpressure, enqueue_task

KEYS = [
    "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
    "QTBSBXVTEAMEQO-UHFFFAOYSA-N",
    "XLYOFNOQVPJJNP-UHFFFAOYSA-N",
    "UHOVQNZJYSORNB-UHFFFAOYSA-N",
    "OKKJLVBELUTLKV-UHFFFAOYSA-N",
    "QUSNBJAOOMFDIB-UHFFFAOYSA-N",
]


def _enqueue_one(key: str, barrier: mp.Barrier, queue_out: mp.Queue) -> None:
    close_old_connections()
    barrier.wait()
    try:
        task = enqueue_task(key)
        queue_out.put(("created", task.inchikey if task else None))
    except RemoteQueueBackpressure:
        queue_out.put(("blocked", key))


@pytest.mark.django_db(transaction=True)
def test_global_queue_depth_is_race_safe(settings):
    settings.REMOTE_MAX_QUEUE_DEPTH = 3
    barrier = mp.Barrier(len(KEYS))
    queue_out: mp.Queue = mp.Queue()
    processes = [mp.Process(target=_enqueue_one, args=(key, barrier, queue_out)) for key in KEYS]
    # Close the parent's DB connection BEFORE forking so each child opens its own
    # fresh socket to the (inherited) test DB. Without this the children share the
    # parent's live socket fd and tearing one down kills the parent's connection.
    connections.close_all()
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    results = [queue_out.get(timeout=1) for _ in processes]
    assert sum(1 for status, _ in results if status == "created") == 3
    assert sum(1 for status, _ in results if status == "blocked") == 3
    assert RemoteTask.objects.filter(state__in=[TaskState.READY]).count() == 3


@pytest.mark.django_db(transaction=True)
def test_backpressured_batch_item_is_retried_by_ingest_worker(settings):
    from django.utils import timezone

    from apps.batches.ingest import parse_text_input
    from apps.batches.models import BatchItem, BatchJob
    from apps.batches.services import create_job, run_pending_jobs
    from apps.core.enums import BatchItemStatus, BatchProcessingState

    settings.REMOTE_MAX_QUEUE_DEPTH = 1
    blocker = RemoteTask.objects.create(inchikey=KEYS[0], state=TaskState.READY)

    job, _secret = create_job(
        parse_text_input("C1CCCCC1"),
        remote_enabled=True,
        remote_principal="principal-a",
    )
    item = BatchItem.objects.get(job=job)
    assert item.status == BatchItemStatus.REMOTE_DELAYED
    assert item.message == "Remote queue is at capacity; retrying later."
    assert job.processing_state == BatchProcessingState.REMOTE_PENDING
    assert RemoteTask.objects.filter(inchikey=item.inchikey).count() == 0

    blocker.state = TaskState.DONE
    blocker.save(update_fields=["state"])

    # Parking on backpressure gates the job's next claim on retry_not_before
    # (a short interval); elapse it so this test covers the retry, not the pacing.
    BatchJob.objects.filter(pk=job.pk).update(retry_not_before=timezone.now())

    assert run_pending_jobs("worker-1", max_jobs=1) == 1
    item.refresh_from_db()
    job.refresh_from_db()
    assert item.status == BatchItemStatus.PENDING_CLASSIFICATION
    assert item.message is None
    assert job.processing_state == BatchProcessingState.REMOTE_PENDING
    assert RemoteTask.objects.filter(inchikey=item.inchikey, state=TaskState.READY).count() == 1


@pytest.mark.django_db(transaction=True)
def test_ingest_worker_stops_delayed_retry_after_queue_backpressure(monkeypatch):
    from apps.batches.models import BatchItem, BatchJob
    from apps.batches.services import (
        _retry_transiently_delayed_remote_items,
        claim_from_job,
    )
    from apps.core.enums import BatchItemStatus, BatchJobStatus, BatchProcessingState

    job = BatchJob.objects.create(
        secret_hash="x",
        status=BatchJobStatus.IN_PROGRESS,
        processing_state=BatchProcessingState.REMOTE_PENDING,
        remote_enabled=True,
        total_count=3,
        remote_pending_count=3,
    )
    items = [
        BatchItem.objects.create(
            job=job,
            row_order=index,
            original_input=key,
            inchikey=key,
            status=BatchItemStatus.REMOTE_DELAYED,
            message="Remote queue is at capacity; retrying later.",
        )
        for index, key in enumerate(KEYS[:3])
    ]
    enqueue_attempts: list[str] = []

    monkeypatch.setattr(
        "apps.batches.services.lookup_current_classification",
        lambda _inchikey: None,
    )

    def reject_enqueue(inchikey, _compound, *, principal=None, priority=0):
        enqueue_attempts.append(inchikey)
        raise RemoteQueueBackpressure("remote queue depth limit reached")

    monkeypatch.setattr(
        "apps.batches.services._get_or_create_remote_task",
        reject_enqueue,
    )

    _retry_transiently_delayed_remote_items(
        claim_from_job(job),
        remote_enabled=True,
        remote_principal=job.remote_principal,
        job_priority=job.priority,
    )

    assert enqueue_attempts == [items[0].inchikey]
