"""Gate 5 / Task 5: provider error taxonomy + result freshness semantics.

Two correctness gaps are closed here:

* The Wishart ENTITY lookup must distinguish a genuine entity MISS (HTTP 404 →
  ``NOT_FOUND``, "try calculating via a structure submit") from a PERMANENT
  input error (HTTP 400/422 → ``PERMANENT_ERROR``, "do NOT submit, surface").
  Conflating them lets a malformed request wrongly trigger a calculation.

* Result freshness must use the query's SUBMIT time as the apply timestamp, not
  ``timezone.now()`` at apply time. Otherwise a LATE-arriving OLD-query result
  could supersede a NEWER one. ``write_live_result`` rejects a write whose
  ``retrieved_at`` is older than the stored event at equal priority as
  ``REJECTED_STALE``; the worker's query-apply path stamps ``retrieved_at`` from
  ``query.submitted_at`` so the stored timestamps reflect query order.

The repo has no conftest/factory framework, so the two helpers below are thin
local fixtures (mirroring Tasks 2-4 tests).
"""
from __future__ import annotations

import httpx
import pytest

from apps.classifications.models import ClassyfireLive
from apps.classifications.result_writer import write_live_result
from apps.core.enums import SourceType
from apps.remote.models import RemoteQuery
from apps.remote.provider import EntityLookupStatus, NormalizedClassification

IK = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"


@pytest.fixture
def wishart_provider_factory():
    """Build a WishartDirectProvider whose HTTP client returns a fixed response.

    Mirrors the ``httpx.MockTransport`` pattern in ``test_provider_contract`` but
    returns an arbitrary ``body_text`` with the requested ``status_code`` so the
    error-status matrix can assert the raw body is carried on every branch.
    """
    from apps.remote.wishart import WishartDirectProvider

    def _factory(*, status_code: int, body_text: str):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, text=body_text)

        provider = WishartDirectProvider(base_url="https://wishart.test")
        transport = httpx.MockTransport(handler)
        provider._client = lambda: httpx.Client(transport=transport)  # type: ignore[method-assign]
        return provider

    return _factory


@pytest.mark.parametrize(
    "status_code,expected",
    [
        (400, EntityLookupStatus.PERMANENT_ERROR),
        (401, EntityLookupStatus.AUTH_FAILURE),
        (403, EntityLookupStatus.AUTH_FAILURE),
        (404, EntityLookupStatus.NOT_FOUND),
        (422, EntityLookupStatus.PERMANENT_ERROR),
        (429, EntityLookupStatus.RETRYABLE_ERROR),
        (500, EntityLookupStatus.RETRYABLE_ERROR),
    ],
)
def test_entity_http_status_matrix(wishart_provider_factory, status_code, expected):
    provider = wishart_provider_factory(status_code=status_code, body_text="bad")
    result = provider.lookup_entity(IK)
    assert result.status == expected
    assert result.raw_text == "bad"


class _RemoteQueryOrderScenario:
    """Apply a newer-query result, then a LATE older-query result.

    Both results land at the SAME source priority (WISHART_LIVE_QUERY = 100). The
    apply timestamp is each query's ``submitted_at`` (NOT wall-clock at apply
    time), so the late older-query result is rejected as STALE and live keeps
    pointing at the newer query — even though it is written/applied last.
    """

    def __init__(self) -> None:
        # The OLDER query was submitted first; the NEWER query second. We apply
        # them OUT of submit order (newer first, older last) to prove freshness
        # follows the submit timestamp, not the apply order / wall clock.
        self.older = RemoteQuery.objects.create(
            remote_query_id="older-query", payload_hash="hash-older"
        )
        self.newer = RemoteQuery.objects.create(
            remote_query_id="newer-query", payload_hash="hash-newer"
        )
        # Guard: auto_now_add must order the two queries as named.
        assert self.older.submitted_at < self.newer.submitted_at

    @staticmethod
    def _norm() -> NormalizedClassification:
        return NormalizedClassification(
            inchikey=IK,
            kingdom=None,
            superclass=None,
            class_=None,
            subclass=None,
            direct_parent=None,
        )

    def apply_newer_then_late_older(self) -> ClassyfireLive:
        write_live_result(
            IK,
            self._norm(),
            source_type=SourceType.WISHART_LIVE_QUERY,
            remote_query_id=self.newer.remote_query_id,
            retrieved_at=self.newer.submitted_at,
        )
        # LATE arrival of the OLDER query's result (older submit time).
        write_live_result(
            IK,
            self._norm(),
            source_type=SourceType.WISHART_LIVE_QUERY,
            remote_query_id=self.older.remote_query_id,
            retrieved_at=self.older.submitted_at,
        )
        return ClassyfireLive.objects.get(inchikey=IK)


@pytest.fixture
def remote_query_order_scenario():
    return _RemoteQueryOrderScenario()


@pytest.mark.django_db
def test_late_old_query_does_not_supersede_newer_query(remote_query_order_scenario):
    live = remote_query_order_scenario.apply_newer_then_late_older()
    assert live.remote_query_id == "newer-query"
