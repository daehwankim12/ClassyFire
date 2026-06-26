"""Gate 5 / Task 2: lease-fenced remote result side effects.

A STALE worker — one whose lease was stolen by a fresh worker after a
crash/stale-lock recovery — must apply NOTHING when it tries to write a result:
no ClassificationResult event, no fan-out of batch items, no task transition. It
must raise :class:`queue.LeaseLost` BEFORE any side effect, because the whole
apply (write live result + fan-out + DONE) runs under ONE lease-fenced
transaction guarded by ``queue.lock_owned_task``.
"""
from __future__ import annotations

import pytest

from apps.batches.models import BatchItem, BatchJob
from apps.classifications.models import ClassificationResult
from apps.compounds.models import CompoundStructure
from apps.core.enums import BatchItemStatus, TaskState
from apps.remote import queue
from apps.remote.circuit import DbCircuitBreaker
from apps.remote.mock import MockProvider, build_raw, parse_wishart_payload, parse_wishart_results
from apps.remote.models import (
    RemoteAttempt,
    RemoteCircuit,
    RemoteQuery,
    RemoteSubmission,
    RemoteTask,
)
from apps.remote.provider import (
    EntityLookupResult,
    EntityLookupStatus,
    NormalizedClassification,
)
from apps.remote.worker import RemoteWorker

pytestmark = pytest.mark.django_db(transaction=True)

IK = "RDHQFKQIGNGIED-MRVPVSSYSA-N"
SMILES = "CCO"


def _make_item(*, status: BatchItemStatus) -> BatchItem:
    job = BatchJob.objects.create(secret_hash="x", total_count=1)
    compound = CompoundStructure.objects.create(
        original_input=SMILES, canonical_smiles=SMILES, inchikey=IK
    )
    return BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=IK,
        inchikey=IK,
        compound=compound,
        status=status,
    )


def _norm() -> NormalizedClassification:
    return NormalizedClassification(
        inchikey=IK,
        kingdom=None,
        superclass=None,
        class_=None,
        subclass=None,
        direct_parent=None,
    )


def test_stale_worker_cannot_write_classification_or_fanout():
    item = _make_item(status=BatchItemStatus.PENDING_CLASSIFICATION)
    RemoteTask.objects.create(
        inchikey=item.inchikey,
        compound=item.compound,
        state=TaskState.READY,
    )

    # The stale worker claims the task, then a recovery sweep (lease=0) reclaims
    # it and a fresh worker re-claims it — stealing the lease (new token + bumped
    # version) out from under the stale holder.
    stale = queue.claim_task("stale-worker")
    assert stale is not None
    queue.recover_stale_locks(lease_seconds=0)
    fresh = queue.claim_task("fresh-worker")
    assert fresh is not None

    norm = _norm()
    worker = RemoteWorker(MockProvider(entities={}), worker_id="stale-worker")

    with pytest.raises(queue.LeaseLost):
        worker.apply_fenced_result_for_test(stale, norm, remote_query_id="Q1")

    # No side effect leaked out before the fence raised.
    assert (
        ClassificationResult.objects.filter(requested_inchikey=item.inchikey).count()
        == 0
    )
    item.refresh_from_db()
    assert item.status == BatchItemStatus.PENDING_CLASSIFICATION
    assert fresh.lock_owner == "fresh-worker"


class _AllowAllLimiter:
    """Permissive duck-typed rate limiter for deterministic fencing tests."""

    class _Bucket:
        def allow(self) -> bool:
            return True

        def penalize(self, retry_after: float | None) -> None:
            pass

    def __init__(self) -> None:
        self.entity = self._Bucket()
        self.submit = self._Bucket()


class _ClosedCircuit:
    def allow(self) -> bool:
        return True

    def record_success(self) -> None:
        pass

    def record_failure(self) -> None:
        pass


class LeaseStealingEntityProvider:
    """An entity provider whose ``lookup_entity`` steals the caller's lease.

    Mid-lookup it simulates a recovery sweep + a fresh worker re-claim, so the
    snapshotted claim the caller carries is now stale. Every entity outcome the
    worker must handle is returned verbatim via ``status`` so the test can assert
    that a STALE entity worker writes NOTHING regardless of provider outcome.
    """

    def __init__(self, *, task: RemoteTask, status: EntityLookupStatus) -> None:
        self._task = task
        self._status = status

    def lookup_entity(self, inchikey: str) -> EntityLookupResult:
        queue.recover_stale_locks(lease_seconds=0)
        fresh = queue.claim_task("fresh-worker")
        assert fresh is not None
        raw = build_raw(inchikey) if self._status == EntityLookupStatus.FOUND else None
        return EntityLookupResult(
            status=self._status,
            inchikey=inchikey,
            raw_json=raw,
            detail="lease stolen mid-lookup",
        )

    def submit_structures(self, structures):  # pragma: no cover - must never run
        raise AssertionError("a stale entity worker must never submit")

    def poll_query(self, remote_query_id):  # pragma: no cover - must never run
        raise AssertionError("a stale entity worker must never poll")

    def parse_result(self, raw_json):
        return parse_wishart_payload(raw_json)

    def parse_results(self, raw_json):
        return parse_wishart_results(raw_json)


@pytest.fixture
def remote_task_with_structure(settings):
    settings.REMOTE_ENABLED = True
    settings.REMOTE_BATCH_SIZE = 1
    settings.REMOTE_POLL_MIN_SECONDS = 1
    compound = CompoundStructure.objects.create(
        original_input=SMILES, canonical_smiles=SMILES, inchikey=IK
    )
    RemoteTask.objects.create(
        inchikey=IK, compound=compound, state=TaskState.READY
    )
    task = queue.claim_task("owner-worker")
    assert task is not None
    return task


@pytest.mark.parametrize(
    "status",
    [
        EntityLookupStatus.FOUND,
        EntityLookupStatus.NOT_FOUND,
        EntityLookupStatus.MISS,
        EntityLookupStatus.AUTH_FAILURE,
        EntityLookupStatus.PERMANENT_ERROR,
        EntityLookupStatus.PROTOCOL_ERROR,
        EntityLookupStatus.RETRYABLE_ERROR,
    ],
)
def test_stale_entity_worker_has_zero_side_effects_for_every_provider_outcome(
    status, remote_task_with_structure
):
    task = remote_task_with_structure
    claim = queue.claim_from_task(task)
    provider = LeaseStealingEntityProvider(task=task, status=status)
    worker = RemoteWorker(
        provider,
        worker_id=str(task.lock_owner),
        rate_limiter=_AllowAllLimiter(),
        circuit=_ClosedCircuit(),
    )

    worker._entity_first(task, claim)

    assert RemoteAttempt.objects.count() == 0
    assert RemoteSubmission.objects.count() == 0
    assert RemoteQuery.objects.count() == 0
    task.refresh_from_db()
    assert task.lock_owner == "fresh-worker"


@pytest.mark.parametrize(
    "status",
    [
        EntityLookupStatus.FOUND,
        EntityLookupStatus.RETRYABLE_ERROR,
    ],
)
def test_stale_entity_worker_does_not_mutate_db_circuit(
    status, remote_task_with_structure
):
    task = remote_task_with_structure
    claim = queue.claim_from_task(task)
    provider = LeaseStealingEntityProvider(task=task, status=status)
    worker = RemoteWorker(
        provider,
        worker_id=str(task.lock_owner),
        rate_limiter=_AllowAllLimiter(),
        circuit=DbCircuitBreaker(failure_threshold=1),
    )

    worker._entity_first(task, claim)

    assert RemoteCircuit.objects.count() == 0
