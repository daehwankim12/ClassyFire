"""Tests for the remote provider boundary (Task 9).

Covers the MockProvider (used by the whole suite) plus the WishartDirectProvider
HTTP outcome classification using httpx's MockTransport — NO real network.
"""

from __future__ import annotations

from datetime import UTC

import httpx
import pytest

from apps.remote.mock import MockProvider, build_query_response, build_raw
from apps.remote.provider import (
    AuthProviderError,
    EntityLookupStatus,
    HttpOutcome,
    NonRetryableProviderError,
    PollStatus,
    ProtocolProviderError,
    RetryableProviderError,
    classify_http_status,
)

IK_A = "RDHQFKQIGNGIED-MRVPVSSYSA-N"
IK_B = "WQZGKKKJIJFFOK-GASJEMHNSA-N"


# ---------------------------------------------------------------------------
# MockProvider
# ---------------------------------------------------------------------------


def test_mock_entity_found():
    provider = MockProvider(entities={IK_A: build_raw(IK_A)})
    result = provider.lookup_entity(IK_A)
    assert result.status == EntityLookupStatus.FOUND
    assert result.raw_json is not None
    norm = provider.parse_result(result.raw_json)
    assert norm.inchikey == IK_A  # InChIKey= prefix stripped
    assert norm.kingdom is not None
    assert norm.kingdom.name == "Organic compounds"


def test_mock_entity_miss():
    provider = MockProvider()
    result = provider.lookup_entity(IK_A)
    assert result.status == EntityLookupStatus.MISS
    assert result.raw_json is None


def test_mock_entity_retryable_with_retry_after():
    provider = MockProvider(entity_retryable_for={IK_A}, entity_retry_after=12.0)
    result = provider.lookup_entity(IK_A)
    assert result.status == EntityLookupStatus.RETRYABLE_ERROR
    assert result.retry_after == 12.0


def test_mock_submit_returns_query_id():
    provider = MockProvider(submit_query_ids=["abc-123"])
    submit = provider.submit_structures([(IK_A, "CCO")])
    assert submit.remote_query_id == "abc-123"
    assert provider.submit_calls == [[(IK_A, "CCO")]]


def test_mock_submit_retryable_raises():
    provider = MockProvider(submit_retryable=True)
    with pytest.raises(RetryableProviderError):
        provider.submit_structures([(IK_A, "CCO")])


def test_mock_submit_non_retryable_raises():
    provider = MockProvider(submit_non_retryable=True)
    with pytest.raises(NonRetryableProviderError):
        provider.submit_structures([(IK_A, "CCO")])


def test_mock_poll_running_then_complete():
    raw = build_raw(IK_A)
    provider = MockProvider(poll_script={"q-1": ["RUNNING", raw]})
    first = provider.poll_query("q-1")
    assert first.status == PollStatus.RUNNING
    second = provider.poll_query("q-1")
    assert second.status == PollStatus.COMPLETE
    assert second.raw_json == raw


def test_mock_poll_retryable():
    provider = MockProvider(poll_retryable_for={"q-9"})
    result = provider.poll_query("q-9")
    assert result.status == PollStatus.RETRYABLE_ERROR


def test_mock_parse_result_key_from_response():
    # Mismatch: response carries a different InChIKey than requested.
    provider = MockProvider()
    raw = build_raw(IK_B)
    norm = provider.parse_result(raw)
    assert norm.inchikey == IK_B


def test_mock_parse_results_yields_every_entity():
    # A completed query response wraps MULTIPLE entities; parse_results must
    # return one normalized classification per entity, each with its own key.
    provider = MockProvider()
    raw = build_query_response([build_raw(IK_A), build_raw(IK_B)])
    norms = provider.parse_results(raw)
    assert [n.inchikey for n in norms] == [IK_A, IK_B]
    # Each carries its own per-entity raw payload, not the whole wrapper.
    assert all("entities" not in (n.raw_json or {}) for n in norms)


def test_mock_parse_results_single_unwrapped_payload():
    # An unwrapped single-entity payload (entity-lookup shape) -> one item.
    provider = MockProvider()
    norms = provider.parse_results(build_raw(IK_A))
    assert [n.inchikey for n in norms] == [IK_A]


def test_mock_parse_results_empty_response():
    provider = MockProvider()
    assert provider.parse_results({}) == []
    assert provider.parse_results(build_query_response([])) == []


def test_mock_parse_results_rejects_surplus_entities_before_normalizing():
    provider = MockProvider()
    raw = build_query_response([build_raw(IK_A), build_raw(IK_B)])

    with pytest.raises(ProtocolProviderError, match="expected at most 1"):
        provider.parse_results(raw, max_entities=1)


# ---------------------------------------------------------------------------
# WishartDirectProvider HTTP outcome classification (httpx MockTransport)
# ---------------------------------------------------------------------------


def _wishart_with(handler):
    from apps.remote.wishart import WishartDirectProvider

    provider = WishartDirectProvider(base_url="https://wishart.test")
    transport = httpx.MockTransport(handler)
    # Patch the client factory to inject the mock transport.
    provider._client = lambda: httpx.Client(transport=transport)  # type: ignore[method-assign]
    return provider


def test_wishart_entity_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=build_raw(IK_A))

    provider = _wishart_with(handler)
    result = provider.lookup_entity(IK_A)
    assert result.status == EntityLookupStatus.FOUND
    assert result.raw_json is not None


def test_wishart_entity_404_is_not_found():
    # A 404 is a GENUINE entity miss (key absent) -> NOT_FOUND, so the worker may
    # try to calculate it via a structure submit. Distinct from PROTOCOL_ERROR.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    provider = _wishart_with(handler)
    result = provider.lookup_entity(IK_A)
    assert result.status == EntityLookupStatus.NOT_FOUND
    assert result.raw_text == "not found"


def test_wishart_entity_429_retryable_with_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "30"})

    provider = _wishart_with(handler)
    result = provider.lookup_entity(IK_A)
    assert result.status == EntityLookupStatus.RETRYABLE_ERROR
    assert result.retry_after == 30.0


def test_wishart_entity_500_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    provider = _wishart_with(handler)
    result = provider.lookup_entity(IK_A)
    assert result.status == EntityLookupStatus.RETRYABLE_ERROR


def test_wishart_submit_5xx_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502)

    provider = _wishart_with(handler)
    with pytest.raises(RetryableProviderError):
        provider.submit_structures([(IK_A, "CCO")])


def test_wishart_submit_400_non_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad structure")

    provider = _wishart_with(handler)
    with pytest.raises(NonRetryableProviderError):
        provider.submit_structures([(IK_A, "CCO")])


def test_wishart_submit_returns_query_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": 7777})

    provider = _wishart_with(handler)
    submit = provider.submit_structures([(IK_A, "CCO")])
    assert submit.remote_query_id == "7777"


def test_wishart_download_query_csv_returns_bytes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/queries/q1.csv"
        return httpx.Response(
            200,
            text="CompoundID,ChemOntID,ClassifiedResults\nq1,NotAvailable,foo: bar\n",
        )

    provider = _wishart_with(handler)

    assert provider.download_query_csv("q1").startswith(b"CompoundID,ChemOntID")


def test_wishart_download_query_csv_429_retryable_with_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "30"})

    provider = _wishart_with(handler)

    with pytest.raises(RetryableProviderError) as excinfo:
        provider.download_query_csv("q1")
    assert excinfo.value.retry_after == 30.0


def test_wishart_poll_running_then_complete():
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(200, json={"classification_status": "In progress"})
        return httpx.Response(
            200, json={"classification_status": "Done", "entities": [build_raw(IK_A)]}
        )

    provider = _wishart_with(handler)
    assert provider.poll_query("q1").status == PollStatus.RUNNING
    done = provider.poll_query("q1")
    assert done.status == PollStatus.COMPLETE
    norm = provider.parse_result(done.raw_json or {})
    assert norm.inchikey == IK_A


def test_wishart_parse_results_unwraps_all_entities():
    # The real provider must surface EVERY entity in a completed query response
    # (not just entities[0]) so every batched task can be resolved.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "classification_status": "Done",
                "entities": [build_raw(IK_A), build_raw(IK_B)],
            },
        )

    provider = _wishart_with(handler)
    done = provider.poll_query("q1")
    assert done.status == PollStatus.COMPLETE
    norms = provider.parse_results(done.raw_json or {})
    assert [n.inchikey for n in norms] == [IK_A, IK_B]


def test_wishart_timeout_is_retryable_entity():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    provider = _wishart_with(handler)
    result = provider.lookup_entity(IK_A)
    assert result.status == EntityLookupStatus.RETRYABLE_ERROR


# ---------------------------------------------------------------------------
# F-10: explicit HTTP status classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code, expected",
    [
        (200, HttpOutcome.SUCCESS),
        (201, HttpOutcome.SUCCESS),
        (404, HttpOutcome.NOT_FOUND),
        (401, HttpOutcome.AUTH_FAILURE),
        (403, HttpOutcome.AUTH_FAILURE),
        (408, HttpOutcome.RETRYABLE),
        (425, HttpOutcome.RETRYABLE),
        (429, HttpOutcome.RETRYABLE),
        (500, HttpOutcome.RETRYABLE),
        (503, HttpOutcome.RETRYABLE),
        (400, HttpOutcome.PERMANENT),
        (422, HttpOutcome.PERMANENT),
        (302, HttpOutcome.PROTOCOL_ERROR),
        (100, HttpOutcome.PROTOCOL_ERROR),
        (600, HttpOutcome.PROTOCOL_ERROR),
    ],
)
def test_classify_http_status_routing(code, expected):
    assert classify_http_status(code) == expected


def test_wishart_entity_401_is_auth_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    result = _wishart_with(handler).lookup_entity(IK_A)
    assert result.status == EntityLookupStatus.AUTH_FAILURE
    assert result.http_status == 401


def test_wishart_entity_unknown_status_is_protocol_error():
    # An unmapped status must NEVER be silently treated as a successful FOUND.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, text="redirect")

    result = _wishart_with(handler).lookup_entity(IK_A)
    assert result.status == EntityLookupStatus.PROTOCOL_ERROR
    assert result.status != EntityLookupStatus.FOUND


def test_wishart_entity_permanent_4xx_is_permanent_error():
    # A definite non-404 4xx (e.g. 422) is a PERMANENT protocol/input error, NOT
    # an entity miss: surface PERMANENT_ERROR (distinct from an unmapped
    # PROTOCOL_ERROR) so the worker NEVER submits a calculation for a malformed
    # request. (Task 17 / F-10 residual.)
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="unprocessable")

    result = _wishart_with(handler).lookup_entity(IK_A)
    assert result.status == EntityLookupStatus.PERMANENT_ERROR
    assert result.raw_text == "unprocessable"


def test_wishart_submit_401_raises_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    with pytest.raises(AuthProviderError):
        _wishart_with(handler).submit_structures([(IK_A, "CCO")])


def test_wishart_submit_unknown_status_raises_protocol_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    with pytest.raises(ProtocolProviderError):
        _wishart_with(handler).submit_structures([(IK_A, "CCO")])


def test_wishart_poll_404_is_not_found_not_complete():
    # A 404 on poll means the query is absent — NOT a successful COMPLETE.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    result = _wishart_with(handler).poll_query("q-missing")
    assert result.status == PollStatus.NOT_FOUND
    assert result.status != PollStatus.COMPLETE


def test_wishart_poll_unknown_status_is_protocol_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    result = _wishart_with(handler).poll_query("q1")
    assert result.status == PollStatus.PROTOCOL_ERROR


def test_wishart_retry_after_delta_seconds():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "45"})

    result = _wishart_with(handler).lookup_entity(IK_A)
    assert result.status == EntityLookupStatus.RETRYABLE_ERROR
    assert result.retry_after == 45.0


def test_wishart_retry_after_http_date_form():
    # ACCEPTANCE (F-10): Retry-After supports BOTH delta-seconds AND HTTP-date.
    import email.utils
    from datetime import datetime, timedelta

    future = datetime.now(UTC) + timedelta(seconds=120)
    http_date = email.utils.format_datetime(future, usegmt=True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": http_date})

    result = _wishart_with(handler).lookup_entity(IK_A)
    assert result.status == EntityLookupStatus.RETRYABLE_ERROR
    assert result.retry_after is not None
    assert 110 < result.retry_after <= 121


def test_wishart_retry_after_malformed_header_is_ignored_for_entity():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "not-a-date"})

    result = _wishart_with(handler).lookup_entity(IK_A)
    assert result.status == EntityLookupStatus.RETRYABLE_ERROR
    assert result.retry_after is None


def test_wishart_retry_after_malformed_header_is_ignored_for_submit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": "not-a-date"})

    with pytest.raises(RetryableProviderError) as excinfo:
        _wishart_with(handler).submit_structures([(IK_A, "CCO")])
    assert excinfo.value.retry_after is None


def test_wishart_retry_after_malformed_header_is_ignored_for_poll():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(408, headers={"Retry-After": "not-a-date"})

    result = _wishart_with(handler).poll_query("q1")
    assert result.status == PollStatus.RETRYABLE_ERROR
    assert result.retry_after is None


def test_wishart_reuses_single_http_client():
    # ACCEPTANCE (F-10): a single httpx.Client is reused across calls (pooling),
    # rather than a fresh per-request client.
    from apps.remote.wishart import WishartDirectProvider

    provider = WishartDirectProvider(base_url="https://wishart.test")
    first = provider._client()
    second = provider._client()
    assert first is second, "the httpx.Client must be cached + reused"
    provider.close()
    # After close the cache is cleared and a new client is created lazily.
    assert provider._http_client is None
    third = provider._client()
    assert third is not first
    provider.close()


def test_wishart_poll_rejects_oversized_content_length(settings):
    settings.WISHART_MAX_RESPONSE_BYTES = 32

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": "33"},
            content=b'{"classification_status":"Done","entities":[]}',
        )

    provider = _wishart_with(handler)
    result = provider.poll_query("q1")
    assert result.status == PollStatus.PROTOCOL_ERROR
    assert "exceeds 32 bytes" in (result.detail or "")
    assert result.raw_json is None


def test_wishart_poll_rejects_stream_past_byte_limit(settings):
    settings.WISHART_MAX_RESPONSE_BYTES = 32

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"classification_status":"Done","entities":[]}',
        )

    provider = _wishart_with(handler)
    result = provider.poll_query("q1")
    assert result.status == PollStatus.PROTOCOL_ERROR
    assert "exceeds 32 bytes" in (result.detail or "")
    assert result.raw_json is None


def test_wishart_contract_limits_alternative_parents():
    from apps.remote.contract import MAX_ALTERNATIVE_PARENTS
    raw = build_raw(IK_A)
    raw["alternative_parents"] = [{}] * (MAX_ALTERNATIVE_PARENTS + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=raw)

    provider = _wishart_with(handler)
    result = provider.lookup_entity(IK_A)
    assert result.status == EntityLookupStatus.PROTOCOL_ERROR
    assert "alternative_parents exceeds" in (result.detail or "")
