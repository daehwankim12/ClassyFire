"""Persistent entity 500 escalates to a structure submit immediately.

Wishart returns HTTP 500 (server fault) for some specific keys — every entity
GET for that key fails, forever. Rather than retrying the doomed GET through
1m/5m/30m backoff windows, the worker escalates a task that HAS a structure to
a fresh calculation submit (a different endpoint, /queries.json, which can
succeed where /entities 500s). Rate-limit 429s, explicit Retry-After responses,
and structureless tasks still just retry.

Style mirrors test_remote_side_effect_fencing: drive ``worker._entity_first``
directly with an explicit claim, which works on the embedded-PG test harness.
"""
from __future__ import annotations

import httpx
import pytest

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
        status=BatchItemStatus.REMOTE_TIMED_OUT,
    )
    RemoteTask.objects.create(inchikey=IK, compound=compound, state=TaskState.READY)
    task = queue.claim_task("owner-worker")
    assert task is not None
    RemoteTask.objects.filter(pk=task.pk).update(attempt=attempt)
    task.refresh_from_db()
    return task


def _run_entity_first(task: RemoteTask, provider) -> None:
    claim = queue.claim_from_task(task)
    worker = RemoteWorker(
        provider,
        worker_id=str(task.lock_owner),
        rate_limiter=_AllowAllLimiter(),
        circuit=_ClosedCircuit(),
    )
    worker._entity_first(task, claim)


def test_persistent_entity_500_escalates_to_submit_immediately():
    task = _claimed_task(with_compound=True, attempt=0)
    provider = _provider_returning(500)

    _run_entity_first(task, provider)

    assert provider.submit_calls == 1


def test_entity_500_with_retry_after_retries_without_submit():
    task = _claimed_task(with_compound=True, attempt=0)
    provider = _provider_returning(500, headers={"Retry-After": "600"})

    _run_entity_first(task, provider)

    assert provider.submit_calls == 0
    task.refresh_from_db()
    assert task.attempt == 1  # Retry-After still takes precedence.


def test_entity_500_without_structure_never_escalates():
    task = _claimed_task(with_compound=False, attempt=queue.MAX_ATTEMPTS - 1)
    provider = _provider_returning(500)

    _run_entity_first(task, provider)

    assert provider.submit_calls == 0
