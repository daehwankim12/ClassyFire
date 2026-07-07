"""Regression: the shared capacity-1 request bucket must not starve submits.

``DbRateLimiter.from_settings()`` aliases entity GETs and submit POSTs to ONE
capacity-1 ``request`` bucket (serialized Wishart traffic). The entity GET that
discovers a cache miss therefore always drains the only token, so the same-tick
submit can never draw one — and parking the task as DEFERRED sends it back
through ``_entity_first``, which burns the next token on ANOTHER GET. Net
effect before the fix: endless entity 404s, zero POSTs.

The worker must instead park the miss-with-structure task as SUBMIT_WAIT and,
when the task is next claimed, go STRAIGHT to the submit (no second entity GET)
so the refilled token is spent on the POST.
"""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.batches.models import BatchItem, BatchJob
from apps.core.enums import BatchItemStatus, QueryState, TaskState
from apps.remote import queue
from apps.remote.circuit import CircuitBreaker
from apps.remote.mock import MockProvider
from apps.remote.models import (
    RemoteQuery,
    RemoteRateBucket,
    RemoteSubmission,
    RemoteTask,
)
from apps.remote.rate_limit import DbRateLimiter
from apps.remote.worker import RemoteWorker

pytestmark = pytest.mark.django_db(transaction=True)

IK_A = "RDHQFKQIGNGIED-MRVPVSSYSA-N"


def _seed_task(inchikey: str = IK_A):
    from apps.compounds.models import CompoundStructure

    job = BatchJob.objects.create(secret_hash="x", total_count=0)
    comp = CompoundStructure.objects.create(
        original_input="CCO",
        canonical_smiles="CCO",
        inchikey=inchikey,
    )
    BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=inchikey,
        inchikey=inchikey,
        compound=comp,
        status=BatchItemStatus.PENDING_CLASSIFICATION,
    )
    queue.enqueue_task(inchikey, compound_id=comp.id)
    return job


def _production_worker(provider, settings) -> RemoteWorker:
    settings.REMOTE_ENABLED = True
    # Slow the shared bucket to one request/min so the same-tick submit can
    # never sneak a refilled token even on a slow test box.
    settings.REMOTE_ENTITY_REQUESTS_PER_MIN = 1
    settings.REMOTE_SUBMITS_PER_MIN = 1
    return RemoteWorker(
        provider,
        worker_id="w-test",
        rate_limiter=DbRateLimiter.from_settings(),
        circuit=CircuitBreaker(failure_threshold=1000),
    )


def _refill_request_bucket() -> None:
    RemoteRateBucket.objects.filter(bucket="request").update(
        tokens=1.0, updated_at=timezone.now(), blocked_until=None
    )


def test_entity_miss_with_shared_bucket_parks_submit_wait(settings):
    _seed_task()
    provider = MockProvider(submit_query_ids=["q-sw"])  # entity MISS, submit ok
    worker = _production_worker(provider, settings)

    assert worker.tick() is True

    task = RemoteTask.objects.get(inchikey=IK_A)
    # The GET drained the only token: no POST can have happened, and the task
    # must wait in SUBMIT_WAIT (not DEFERRED, which would re-run the GET).
    assert provider.entity_calls == [IK_A]
    assert provider.submit_calls == []
    assert not RemoteSubmission.objects.exists()
    assert task.state == TaskState.SUBMIT_WAIT
    assert task.next_run_at is not None and task.next_run_at > timezone.now()


def test_submit_wait_task_resumes_straight_to_submit(settings):
    _seed_task()
    provider = MockProvider(submit_query_ids=["q-sw"])
    worker = _production_worker(provider, settings)
    assert worker.tick() is True
    assert RemoteTask.objects.get(inchikey=IK_A).state == TaskState.SUBMIT_WAIT

    # A token refilled and the park delay elapsed.
    _refill_request_bucket()
    RemoteTask.objects.update(next_run_at=timezone.now())

    assert worker.tick() is True

    task = RemoteTask.objects.get(inchikey=IK_A)
    # The refilled token must be spent on the POST — not on a second GET.
    assert provider.entity_calls == [IK_A]
    assert provider.submit_calls and provider.submit_calls[0][0][0] == IK_A
    assert task.state == TaskState.POLL_WAIT
    query = RemoteQuery.objects.get(remote_query_id="q-sw")
    assert query.state == QueryState.SUBMITTED


def test_submit_wait_keeps_waiting_when_token_still_missing(settings):
    _seed_task()
    provider = MockProvider(submit_query_ids=["q-sw"])
    worker = _production_worker(provider, settings)
    assert worker.tick() is True

    # Park delay elapsed but NO token refilled: stay in SUBMIT_WAIT without
    # burning a GET, so the eventual token still goes to the POST.
    RemoteTask.objects.update(next_run_at=timezone.now())

    assert worker.tick() is True

    task = RemoteTask.objects.get(inchikey=IK_A)
    assert provider.entity_calls == [IK_A]
    assert provider.submit_calls == []
    assert task.state == TaskState.SUBMIT_WAIT
