"""An entity 500 escalates to a structure submit without a global provider gate.

Wishart returns HTTP 500 (server fault) for some specific keys — every entity
GET for that key may fail repeatedly while other keys remain healthy. A task
that HAS a structure therefore proceeds to a fresh calculation submit (a
different endpoint, /queries.json, which can succeed where /entities 500s).
Rate-limit 429s still pace the provider globally, while an entity 5xx remains
task-local. Structureless tasks move to NEED_STRUCTURE instead of blocking the
remote queue on entity backoff.

Style mirrors test_remote_side_effect_fencing: drive ``worker._entity_first``
directly with an explicit claim, which works on the embedded-PG test harness.
"""
from __future__ import annotations

import httpx
import pytest

from apps.batches.models import BatchItem, BatchJob
from apps.compounds.models import CompoundStructure
from apps.core.enums import BatchItemStatus, SubmissionState, TaskState
from apps.remote import queue
from apps.remote.models import RemoteSubmission, RemoteTask
from apps.remote.worker import RemoteWorker

pytestmark = pytest.mark.django_db(transaction=True)

IK = "RDHQFKQIGNGIED-MRVPVSSYSA-N"
SMILES = "CCO"


class _AllowAllLimiter:
    class _Bucket:
        def __init__(self) -> None:
            self.penalties: list[float] = []

        def allow(self) -> bool:
            return True

        def penalize(self, retry_after: float | None) -> None:
            if retry_after is not None:
                self.penalties.append(retry_after)

    def __init__(self) -> None:
        self.entity = self._Bucket()
        self.submit = self._Bucket()


def _provider_returning(status_code: int, *, headers: dict[str, str] | None = None):
    """A WishartDirectProvider whose transport returns ``status_code`` for every
    request, counting how many times ``submit_structures`` is invoked."""
    from apps.remote.wishart import WishartDirectProvider

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers=headers,
            json={"status": status_code},
        )

    provider = WishartDirectProvider(base_url="https://wishart.test")
    transport = httpx.MockTransport(handler)
    provider._client = lambda: httpx.Client(transport=transport)  # type: ignore[method-assign]
    provider.submit_calls = 0  # type: ignore[attr-defined]
    real_submit = provider.submit_structures

    def counting_submit(structures):
        provider.submit_calls += 1  # type: ignore[attr-defined]
        return real_submit(structures)

    provider.submit_structures = counting_submit  # type: ignore[method-assign]
    return provider


def _claimed_task(*, with_compound: bool, attempt: int) -> RemoteTask:
    compound = (
        CompoundStructure.objects.create(
            original_input=SMILES, canonical_smiles=SMILES, inchikey=IK
        )
        if with_compound
        else None
    )
    job = BatchJob.objects.create(secret_hash="x", total_count=1)
    BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=IK,
        inchikey=IK,
        compound=compound,
        status=BatchItemStatus.PENDING_CLASSIFICATION,
    )
    RemoteTask.objects.create(inchikey=IK, compound=compound, state=TaskState.READY)
    task = queue.claim_task("owner-worker")
    assert task is not None
    RemoteTask.objects.filter(pk=task.pk).update(attempt=attempt)
    task.refresh_from_db()
    return task


def _run_entity_first(task: RemoteTask, provider, limiter=None) -> None:
    claim = queue.claim_from_task(task)
    worker = RemoteWorker(
        provider,
        worker_id=str(task.lock_owner),
        rate_limiter=limiter or _AllowAllLimiter(),
    )
    worker._entity_first(task, claim)


def test_first_entity_500_with_structure_submits_immediately():
    task = _claimed_task(with_compound=True, attempt=0)
    provider = _provider_returning(500)

    _run_entity_first(task, provider)

    assert provider.submit_calls == 1
    task.refresh_from_db()
    # The submit itself got a 500, which may follow a commit upstream: the
    # intent is held UNKNOWN and the task parked for reconciliation rather than
    # auto-retried into a possible duplicate query.
    assert task.state == TaskState.DEFERRED
    assert task.next_run_at is None
    assert RemoteSubmission.objects.filter(state=SubmissionState.UNKNOWN).count() == 1


def test_entity_500_with_retry_after_submits_without_honoring_retry_after():
    task = _claimed_task(with_compound=True, attempt=0)
    provider = _provider_returning(500, headers={"Retry-After": "600"})
    limiter = _AllowAllLimiter()

    _run_entity_first(task, provider, limiter=limiter)

    assert provider.submit_calls == 1
    assert limiter.entity.penalties == []
    assert limiter.submit.penalties == []
    task.refresh_from_db()
    # Neither the entity nor the (ambiguous) submit 500 honours Retry-After.
    assert task.state == TaskState.DEFERRED
    assert task.next_run_at is None


def test_third_entity_500_with_retry_after_escalates_to_submit():
    task = _claimed_task(with_compound=True, attempt=2)
    provider = _provider_returning(500, headers={"Retry-After": "300"})

    _run_entity_first(task, provider)

    assert provider.submit_calls == 1


def test_entity_500_without_structure_marks_need_structure():
    task = _claimed_task(with_compound=False, attempt=queue.MAX_ATTEMPTS - 1)
    provider = _provider_returning(500)

    _run_entity_first(task, provider)

    assert provider.submit_calls == 0
    task.refresh_from_db()
    assert task.state == TaskState.DONE
    item = BatchItem.objects.get(inchikey=IK)
    assert item.status == BatchItemStatus.NEED_STRUCTURE


def test_entity_500_with_retry_after_without_structure_marks_need_structure():
    task = _claimed_task(with_compound=False, attempt=0)
    provider = _provider_returning(500, headers={"Retry-After": "300"})
    limiter = _AllowAllLimiter()

    _run_entity_first(task, provider, limiter=limiter)

    assert provider.submit_calls == 0
    assert limiter.entity.penalties == []
    task.refresh_from_db()
    assert task.state == TaskState.DONE
    item = BatchItem.objects.get(inchikey=IK)
    assert item.status == BatchItemStatus.NEED_STRUCTURE


def test_repeated_entity_500_with_retry_after_without_structure_marks_need_structure():
    task = _claimed_task(with_compound=False, attempt=4)
    provider = _provider_returning(500, headers={"Retry-After": "300"})

    _run_entity_first(task, provider)

    assert provider.submit_calls == 0
    task.refresh_from_db()
    assert task.state == TaskState.DONE
    item = BatchItem.objects.get(inchikey=IK)
    assert item.status == BatchItemStatus.NEED_STRUCTURE


def test_exhausted_entity_500_still_escalates_to_submit():
    task = _claimed_task(with_compound=True, attempt=queue.MAX_ATTEMPTS - 1)
    provider = _provider_returning(500)

    _run_entity_first(task, provider)

    assert provider.submit_calls == 1
