from __future__ import annotations

import pytest
from django.utils import timezone

from apps.batches.models import BatchItem
from apps.batches.services import create_job
from apps.classifications.models import ClassyfireLive
from apps.compounds.models import CompoundStructure
from apps.core.enums import SOURCE_PRIORITY, BatchItemStatus, SourceType, TaskState
from apps.remote import queue
from apps.remote.models import RemoteTask
from apps.remote.worker import RemoteWorker

pytestmark = pytest.mark.django_db(transaction=True)

IK = "DACJALPQSPXULN-UHFFFAOYSA-N"
SMILES = "CCO"


def _row(inchikey: str = IK) -> dict[str, object]:
    return {"raw": inchikey, "row_index": 0}


def test_negative_cache_hard_fails_after_three_remote_failures():
    from apps.remote.models import RemoteNegativeCache
    from apps.remote.negative_cache import record_negative_failure

    for _ in range(3):
        record_negative_failure(
            IK,
            operation="SUBMIT",
            http_status=500,
            detail="Wishart submit failed",
        )

    cached = RemoteNegativeCache.objects.get(inchikey=IK)
    assert cached.failure_count == 3
    assert cached.state == RemoteNegativeCache.State.HARD_FAILED
    assert cached.last_failure_operation == "SUBMIT"
    assert cached.last_http_status == 500


def test_hard_negative_without_manual_fallback_finishes_new_rows_without_remote_task():
    from apps.remote.negative_cache import record_negative_failure

    for _ in range(3):
        record_negative_failure(IK, operation="POLL", http_status=500, detail="missing")

    job, _secret = create_job([_row()], remote_enabled=True)

    item = BatchItem.objects.get(job=job)
    assert item.status == BatchItemStatus.REMOTE_TIMED_OUT
    assert item.message == "Wishart negative cache hard-failed for this InChIKey."
    assert not RemoteTask.objects.filter(inchikey=IK).exclude(state=TaskState.DONE).exists()


def test_manual_fallback_resolves_hard_negative_without_writing_live_cache():
    from apps.classifications.models import ClassyfireLive
    from apps.remote.models import ManualClassificationOverride, RemoteNegativeCache
    from apps.remote.negative_cache import create_manual_fallback, record_negative_failure

    for _ in range(3):
        record_negative_failure(IK, operation="SUBMIT", http_status=500, detail="failed")
    negative = RemoteNegativeCache.objects.get(inchikey=IK)
    create_manual_fallback(
        negative,
        labels={"kingdom": {"chemont_id": None, "name": "Manual Kingdom"}},
        actor="admin",
        admin_note="temporary curation",
    )

    job, _secret = create_job([_row()], remote_enabled=True)

    item = BatchItem.objects.get(job=job)
    assert item.status == BatchItemStatus.FOUND_LIVE_CACHE
    assert item.classification_snapshot["source_type"] == "ADMIN_MANUAL_FALLBACK"
    assert item.classification_snapshot["kingdom"]["name"] == "Manual Kingdom"
    assert item.resolved_classification_id is None
    assert ManualClassificationOverride.objects.filter(inchikey=IK).exists()
    assert not ClassyfireLive.objects.filter(inchikey=IK).exists()


def test_mark_resolved_without_wishart_live_keeps_manual_fallback():
    from apps.remote.models import ManualClassificationOverride, RemoteNegativeCache
    from apps.remote.negative_cache import (
        create_manual_fallback,
        mark_negative_resolved_from_wishart,
        record_negative_failure,
    )

    for _ in range(3):
        record_negative_failure(IK, operation="SUBMIT", http_status=500, detail="failed")
    negative = RemoteNegativeCache.objects.get(inchikey=IK)
    create_manual_fallback(
        negative,
        labels={"kingdom": {"chemont_id": None, "name": "Manual Kingdom"}},
        actor="admin",
    )

    mark_negative_resolved_from_wishart(IK)

    negative.refresh_from_db()
    assert negative.state == RemoteNegativeCache.State.HARD_FAILED
    assert ManualClassificationOverride.objects.filter(inchikey=IK).exists()


def test_wishart_live_result_resolves_negative_cache_and_deletes_manual_fallback():
    from apps.remote.models import ManualClassificationOverride, RemoteNegativeCache
    from apps.remote.negative_cache import (
        create_manual_fallback,
        mark_negative_resolved_from_wishart,
        record_negative_failure,
    )

    for _ in range(3):
        record_negative_failure(IK, operation="SUBMIT", http_status=500, detail="failed")
    negative = RemoteNegativeCache.objects.get(inchikey=IK)
    create_manual_fallback(
        negative,
        labels={"kingdom": {"chemont_id": None, "name": "Manual Kingdom"}},
        actor="admin",
    )
    ClassyfireLive.objects.create(
        inchikey=IK,
        source_type=SourceType.WISHART_LIVE_QUERY,
        source_priority=SOURCE_PRIORITY[SourceType.WISHART_LIVE_QUERY],
        retrieved_at=timezone.now(),
        labels={"kingdom": {"chemont_id": None, "name": "Wishart Kingdom"}},
    )

    mark_negative_resolved_from_wishart(IK)

    negative.refresh_from_db()
    assert negative.state == RemoteNegativeCache.State.RESOLVED
    assert not ManualClassificationOverride.objects.filter(inchikey=IK).exists()


def test_resolved_negative_restarts_failure_count_on_new_failure():
    from apps.remote.models import RemoteNegativeCache
    from apps.remote.negative_cache import record_negative_failure

    negative = RemoteNegativeCache.objects.create(
        inchikey=IK,
        state=RemoteNegativeCache.State.RESOLVED,
        failure_count=3,
    )

    record_negative_failure(
        negative.inchikey,
        operation="SUBMIT",
        http_status=500,
        detail="new failure after resolved state",
    )

    negative.refresh_from_db()
    assert negative.state == RemoteNegativeCache.State.CANDIDATE
    assert negative.failure_count == 1


class _AllowAllLimiter:
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


def _claimed_structure_task() -> RemoteTask:
    compound = CompoundStructure.objects.create(
        original_input=SMILES,
        canonical_smiles=SMILES,
        inchikey=IK,
    )
    job, _secret = create_job([{"raw": SMILES, "row_index": 0}], remote_enabled=False)
    item = BatchItem.objects.get(job=job)
    item.inchikey = IK
    item.compound = compound
    item.status = BatchItemStatus.PENDING_CLASSIFICATION
    item.save(update_fields=["inchikey", "compound", "status"])
    RemoteTask.objects.create(inchikey=IK, compound=compound, state=TaskState.READY)
    task = queue.claim_task("negative-worker")
    assert task is not None
    return task


def test_worker_submit_failure_hard_fails_on_third_negative_count():
    from apps.remote.models import RemoteNegativeCache
    from apps.remote.negative_cache import record_negative_failure
    from apps.remote.provider import RetryableProviderError

    class SubmitFailProvider:
        def submit_structures(self, structures):
            raise RetryableProviderError("submit down", http_status=500)

    record_negative_failure(IK, operation="SUBMIT", http_status=500, detail="first")
    record_negative_failure(IK, operation="SUBMIT", http_status=500, detail="second")
    task = _claimed_structure_task()
    worker = RemoteWorker(
        SubmitFailProvider(),
        worker_id=str(task.lock_owner),
        rate_limiter=_AllowAllLimiter(),
        circuit=_ClosedCircuit(),
    )

    worker._submit_batch(task, queue.claim_from_task(task))

    negative = RemoteNegativeCache.objects.get(inchikey=IK)
    assert negative.state == RemoteNegativeCache.State.HARD_FAILED
    assert negative.failure_count == 3
    assert BatchItem.objects.get(inchikey=IK).status == BatchItemStatus.REMOTE_TIMED_OUT


def test_worker_wishart_success_deletes_manual_fallback():
    from apps.remote.models import ManualClassificationOverride, RemoteNegativeCache
    from apps.remote.negative_cache import create_manual_fallback, record_negative_failure
    from apps.remote.provider import ChemOntSlot, NormalizedClassification

    for _ in range(3):
        record_negative_failure(IK, operation="SUBMIT", http_status=500, detail="failed")
    negative = RemoteNegativeCache.objects.get(inchikey=IK)
    create_manual_fallback(
        negative,
        labels={"kingdom": {"chemont_id": None, "name": "Manual Kingdom"}},
        actor="admin",
    )
    manual_job, _secret = create_job([_row()], remote_enabled=True)
    manual_item = BatchItem.objects.get(job=manual_job)
    assert manual_item.classification_snapshot["source_type"] == "ADMIN_MANUAL_FALLBACK"
    task = _claimed_structure_task()
    norm = NormalizedClassification(
        inchikey=IK,
        kingdom=ChemOntSlot(chemont_id=None, name="Wishart Kingdom"),
        superclass=None,
        class_=None,
        subclass=None,
        direct_parent=None,
        raw_json={"inchikey": IK},
    )
    worker = RemoteWorker(
        object(),
        worker_id=str(task.lock_owner),
        rate_limiter=_AllowAllLimiter(),
        circuit=_ClosedCircuit(),
    )

    worker.apply_fenced_result_for_test(task, norm, remote_query_id="manual-cleanup")

    negative.refresh_from_db()
    assert negative.state == RemoteNegativeCache.State.RESOLVED
    assert not ManualClassificationOverride.objects.filter(inchikey=IK).exists()
    manual_item.refresh_from_db()
    assert manual_item.classification_snapshot["source_type"] == "WISHART_LIVE_QUERY"
    assert manual_item.classification_snapshot["kingdom"]["name"] == "Wishart Kingdom"
    assert manual_item.resolved_classification_id is not None
