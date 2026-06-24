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
from apps.remote.mock import MockProvider
from apps.remote.models import RemoteTask
from apps.remote.provider import NormalizedClassification
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
