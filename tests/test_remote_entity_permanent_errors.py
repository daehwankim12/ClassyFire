"""Task 17 (F-10 residual): a PERMANENT entity-lookup error (a non-404 4xx such
as 400/422 — a bad key / protocol violation) must NEVER escalate to a structure
submit/calculation. Only a GENUINE miss (HTTP 404 -> NOT_FOUND, or the
provider-agnostic MISS) may route onward to a submit. Otherwise a malformed
request burns a remote calculation.

The harness drives the REAL ``RemoteWorker`` against a REAL ``WishartDirectProvider``
whose httpx client is backed by an ``httpx.MockTransport`` returning a fixed
status + body (no network). ``submit_structures`` is wrapped so the test can
assert it was NEVER called for a permanent entity error. A submittable structure
(compound) is enqueued so that — were the worker to treat the permanent error as
a miss — it WOULD attempt a submit; the assertion ``submit_calls == 0`` proves
it does not.
"""
from __future__ import annotations

import httpx
import pytest

from apps.remote import queue
from apps.remote.circuit import CircuitBreaker
from apps.remote.models import RemoteAttempt
from apps.remote.rate_limit import RateLimiter
from apps.remote.worker import RemoteWorker

IK = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"


class _EntityErrorHarness:
    """Builds providers that return a fixed HTTP status/body on entity lookup and
    runs a single worker tick against a freshly enqueued, submittable task."""

    inchikey = IK

    def _compound(self):
        from apps.compounds.models import CompoundStructure

        return CompoundStructure.objects.create(
            original_input="CCO",
            canonical_smiles="CCO",
            inchikey=self.inchikey,
        )

    def provider_for_status(self, status_code: int, body: str):
        from apps.remote.wishart import WishartDirectProvider

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, text=body)

        provider = WishartDirectProvider(base_url="https://wishart.test")
        transport = httpx.MockTransport(handler)
        provider._client = lambda: httpx.Client(transport=transport)  # type: ignore[method-assign]

        # Count submit attempts WITHOUT issuing one: a permanent entity error must
        # never reach this. If it ever did, the MockTransport would 4xx the POST
        # too, but we never want to get that far.
        provider.submit_calls = 0  # type: ignore[attr-defined]
        real_submit = provider.submit_structures

        def counting_submit(structures):  # pragma: no cover - must not be called
            provider.submit_calls += 1  # type: ignore[attr-defined]
            return real_submit(structures)

        provider.submit_structures = counting_submit  # type: ignore[method-assign]
        return provider

    def run(self, provider) -> None:
        comp = self._compound()
        queue.enqueue_task(self.inchikey, compound_id=comp.id)
        worker = RemoteWorker(
            provider,
            worker_id="w-perm",
            rate_limiter=RateLimiter(1000, 1000),
            circuit=CircuitBreaker(failure_threshold=1000),
        )
        worker.tick()


@pytest.fixture
def entity_error_harness():
    return _EntityErrorHarness()


@pytest.mark.django_db(transaction=True)
def test_entity_422_does_not_trigger_submit(entity_error_harness):
    provider = entity_error_harness.provider_for_status(422, body="not json")
    entity_error_harness.run(provider)
    assert provider.submit_calls == 0
    attempt = RemoteAttempt.objects.latest("id")
    assert attempt.classification in {"PROTOCOL_ERROR", "PERMANENT"}
    assert attempt.raw_body_text == "not json"


@pytest.mark.django_db(transaction=True)
def test_entity_400_does_not_trigger_submit(entity_error_harness):
    provider = entity_error_harness.provider_for_status(400, body='{"error":"bad key"}')
    entity_error_harness.run(provider)
    assert provider.submit_calls == 0
    attempt = RemoteAttempt.objects.latest("id")
    assert attempt.http_status == 400
