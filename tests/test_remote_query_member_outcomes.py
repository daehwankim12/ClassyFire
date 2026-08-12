"""Task 15: durable per-member apply outcomes for a multi-member query.

A single /queries submission batches several structures into ONE query. When a
worker polls that query to COMPLETE it may only own SOME of the batched members
(the others are LOCKED by other workers after a recovery/rebalance). The worker
must NOT silently skip an unowned sibling: it parks that member's
``RemoteQueryItem`` as ``PENDING_OWNER`` and leaves the query OPEN. The query
goes COMPLETE only once EVERY member reaches a terminal apply state — so the
sibling's own owner finishes it on its later poll.

The harness drives the REAL ``RemoteWorker`` poll/apply path (mirroring the
fake-provider fixtures in ``test_remote_queue.py``). It builds the initial
two-member fixture directly, but the ``APPLIED``/``PENDING_OWNER`` transitions
under test come ONLY from worker code — the harness never sets ``apply_state``.
"""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.batches.models import BatchItem, BatchJob
from apps.compounds.models import CompoundStructure
from apps.core.enums import (
    BatchItemStatus,
    QueryState,
    RemoteQueryItemApplyState,
    TaskState,
)
from apps.remote import queue
from apps.remote.mock import MockProvider, build_query_response, build_raw
from apps.remote.models import RemoteQuery, RemoteQueryItem, RemoteTask
from apps.remote.rate_limit import RateLimiter
from apps.remote.worker import RemoteWorker

IK_A = "RDHQFKQIGNGIED-MRVPVSSYSA-N"
IK_B = "WQZGKKKJIJFFOK-GASJEMHNSA-N"


class _QueryCompleteHarness:
    """Build a two-member SUBMITTED query and drive real worker poll/apply."""

    def __init__(self) -> None:
        # One scriptable provider whose single query returns BOTH entities.
        self.response = build_query_response([build_raw(IK_A), build_raw(IK_B)])
        self.provider = MockProvider(
            submit_query_ids=["q-batch"],
            poll_script={"q-batch": [self.response]},
        )
        self.query: RemoteQuery | None = None

    def make_two_member_query(
        self, *, response_inchikeys: list[str] | None = None
    ) -> tuple[RemoteQuery, RemoteTask, RemoteTask]:
        if response_inchikeys is not None:
            self.response = build_query_response([
                build_raw(ik) for ik in response_inchikeys
            ])
            self.provider.poll_script = {"q-batch": [self.response]}

        job = BatchJob.objects.create(secret_hash="x", total_count=2)
        query = RemoteQuery.objects.create(
            remote_query_id="q-batch",
            payload_hash="payload-batch",
            state=QueryState.SUBMITTED,
        )
        tasks: list[RemoteTask] = []
        for row, ik in enumerate((IK_A, IK_B)):
            compound = CompoundStructure.objects.create(
                original_input="CCO", canonical_smiles="CCO", inchikey=ik
            )
            BatchItem.objects.create(
                job=job,
                row_order=row,
                original_input=ik,
                inchikey=ik,
                compound=compound,
                status=BatchItemStatus.PENDING_CLASSIFICATION,
            )
            task = RemoteTask.objects.create(
                inchikey=ik,
                compound=compound,
                state=TaskState.POLL_WAIT,
            )
            RemoteQueryItem.objects.create(
                query=query, task=task, compound=compound, inchikey=ik
            )
            tasks.append(task)
        self.query = query
        return query, tasks[0], tasks[1]

    def _worker(self, worker_id: str) -> RemoteWorker:
        return RemoteWorker(
            self.provider,
            worker_id=worker_id,
            rate_limiter=RateLimiter(1000, 1000),
        )

    def poll_as(self, worker_id: str, task: RemoteTask) -> None:
        """Drive the real poll → apply path for ``task`` as ``worker_id``."""
        task.refresh_from_db()
        worker = self._worker(worker_id)
        worker._poll_task(task, queue.claim_from_task(task))

    def apply_pending_member_as(self, worker_id: str, task: RemoteTask) -> None:
        """The owner of a PENDING_OWNER member polls + applies it later."""
        self.poll_as(worker_id, task)


@pytest.fixture
def query_complete_harness(settings):
    settings.REMOTE_BATCH_SIZE = 2
    return _QueryCompleteHarness()


@pytest.mark.django_db(transaction=True)
def test_query_complete_does_not_silently_skip_unowned_sibling(query_complete_harness):
    query, task_a, task_b = query_complete_harness.make_two_member_query()
    task_a.lock_owner = "worker-a"
    task_a.lease_version = 1
    task_a.locked_at = timezone.now()
    task_a.state = TaskState.LOCKED
    task_a.save()
    task_b.lock_owner = "worker-b"
    task_b.lease_version = 2
    task_b.locked_at = timezone.now()
    task_b.state = TaskState.LOCKED
    task_b.save()

    query_complete_harness.poll_as("worker-a", task_a)

    query.refresh_from_db()
    item_a = RemoteQueryItem.objects.get(query=query, task=task_a)
    item_b = RemoteQueryItem.objects.get(query=query, task=task_b)
    assert item_a.apply_state == "APPLIED"
    assert item_b.apply_state == "PENDING_OWNER"
    assert query.state != QueryState.COMPLETE

    query_complete_harness.apply_pending_member_as("worker-b", task_b)
    item_b.refresh_from_db()
    query.refresh_from_db()
    assert item_b.apply_state == "APPLIED"
    assert query.state == QueryState.COMPLETE


@pytest.mark.django_db(transaction=True)
def test_stale_pending_owner_park_cannot_clobber_applied_member(
    query_complete_harness,
):
    query, task_a, task_b = query_complete_harness.make_two_member_query()
    RemoteQueryItem.objects.filter(query=query, task=task_a).update(
        apply_state="APPLIED",
        applied_at=timezone.now(),
        apply_detail="already applied",
    )
    RemoteQueryItem.objects.filter(query=query, task=task_b).update(
        apply_state="APPLIED",
        applied_at=timezone.now(),
        apply_detail="applied by owner",
    )

    stale_member_b = RemoteTask.objects.get(pk=task_b.pk)
    stale_member_b.lock_owner = "worker-b"
    worker = query_complete_harness._worker("worker-a")

    worker._park_member_pending_owner(query, stale_member_b)
    worker._complete_query_if_terminal(query)

    item_b = RemoteQueryItem.objects.get(query=query, task=task_b)
    query.refresh_from_db()
    assert item_b.apply_state == "APPLIED"
    assert item_b.apply_detail == "applied by owner"
    assert query.state == QueryState.COMPLETE


@pytest.mark.django_db(transaction=True)
def test_ragged_complete_response_marks_missing_member_terminal(
    query_complete_harness,
):
    query, task_a, task_b = query_complete_harness.make_two_member_query(
        response_inchikeys=[IK_A]
    )
    task_a.lock_owner = "worker-a"
    task_a.lease_version = 1
    task_a.locked_at = timezone.now()
    task_a.state = TaskState.LOCKED
    task_a.save()
    task_b.state = TaskState.POLL_WAIT
    task_b.save()

    query_complete_harness.poll_as("worker-a", task_a)

    query.refresh_from_db()
    item_a = RemoteQueryItem.objects.get(query=query, task=task_a)
    item_b = RemoteQueryItem.objects.get(query=query, task=task_b)
    task_b.refresh_from_db()

    assert item_a.apply_state == RemoteQueryItemApplyState.APPLIED
    assert item_b.apply_state == RemoteQueryItemApplyState.FAILED
    assert "missing from completed provider response" in item_b.apply_detail
    assert item_b.applied_at is not None
    assert query.state == QueryState.COMPLETE
    assert task_b.state == TaskState.RETRY_WAIT
