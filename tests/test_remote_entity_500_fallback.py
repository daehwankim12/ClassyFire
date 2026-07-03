"""Persistent entity 500 escalates to a structure submit after confirmation.

Wishart returns HTTP 500 (server fault) for some specific keys — every entity
GET for that key may fail repeatedly. The worker confirms that the entity
endpoint is persistently bad for the key, then escalates a task that HAS a
structure to a fresh calculation submit (a different endpoint, /queries.json,
which can succeed where /entities 500s). Rate-limit 429s still retry, but entity
5xx failures ignore Retry-After so they cannot keep the queue in 5-minute waits
forever. Structureless tasks move to NEED_STRUCTURE instead of blocking the
remote queue on entity backoff.

Style mirrors test_remote_side_effect_fencing: drive ``worker._entity_first``
directly with an explicit claim, which works on the embedded-PG test harness.
"""
from __future__ import annotations

import httpx
import pytest
from django.utils import timezone

from apps.batches.models import BatchItem, BatchJob
from apps.compounds.models import CompoundStructure
from apps.core.enums import BatchItemStatus, TaskState
from apps.remote import queue
from apps.remote.models import RemoteTask
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


class _ClosedCircuit:
    def allow(self) -> bool:
        return True

    def record_success(self) -> None:
        pass

    def record_failure(self) -> None:
        pass


class _OpeningCircuit:
    def __init__(self) -> None:
        self.open = False

    def allow(self) -> bool:
        return not self.open

    def record_success(self) -> None:
        self.open = False

    def record_failure(self) -> None:
        self.open = True


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


def _run_entity_first(task: RemoteTask, provider, circuit=None, limiter=None) -> None:
    claim = queue.claim_from_task(task)
    worker = RemoteWorker(
        provider,
        worker_id=str(task.lock_owner),
        rate_limiter=limiter or _AllowAllLimiter(),
        circuit=circuit or _ClosedCircuit(),
    )
    worker._entity_first(task, claim)


def test_first_entity_500_with_structure_retries_entity_before_submit():
    task = _claimed_task(with_compound=True, attempt=0)
    provider = _provider_returning(500)

    _run_entity_first(task, provider)

    assert provider.submit_calls == 0
    task.refresh_from_db()
    assert task.state == TaskState.RETRY_WAIT
    assert task.attempt == 1


def test_entity_500_with_retry_after_retries_without_honoring_retry_after():
    task = _claimed_task(with_compound=True, attempt=0)
    provider = _provider_returning(500, headers={"Retry-After": "600"})
    limiter = _AllowAllLimiter()

    _run_entity_first(task, provider, limiter=limiter)

    assert provider.submit_calls == 0
    assert limiter.entity.penalties == []
    assert limiter.submit.penalties == []
    task.refresh_from_db()
    assert task.attempt == 1
    assert task.next_run_at is not None
    assert (task.next_run_at - timezone.now()).total_seconds() < 120


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


def test_entity_500_that_opens_circuit_still_escalates_to_submit():
    """The 500 that trips the breaker must not veto its own escalation.

    In production a burst of per-key 500s opened the circuit; the tripping key
    then won every HALF_OPEN probe, its 500 re-opened the circuit, and the
    escalate branch's circuit re-check deferred it for lease_seconds (300s) —
    an endless 5-minute loop with the submit bypass never running. The submit
    outcome, not the definitive per-key entity 500, must drive the breaker.
    """
    task = _claimed_task(with_compound=True, attempt=2)
    provider = _provider_returning(500)
    circuit = _OpeningCircuit()

    _run_entity_first(task, provider, circuit=circuit)

    assert provider.submit_calls == 1
    assert circuit.open


def test_exhausted_entity_500_escalates_to_submit_after_circuit_opens():
    task = _claimed_task(with_compound=True, attempt=queue.MAX_ATTEMPTS - 1)
    provider = _provider_returning(500)

    _run_entity_first(task, provider, circuit=_OpeningCircuit())

    assert provider.submit_calls == 1
