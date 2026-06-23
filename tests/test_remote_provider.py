"""Tests for the remote provider boundary (Task 9).

Covers the MockProvider (used by the whole suite) plus the WishartDirectProvider
HTTP outcome classification using httpx's MockTransport — NO real network.
"""
from __future__ import annotations

import httpx
import pytest

from apps.remote.mock import MockProvider, build_query_response, build_raw
from apps.remote.provider import (
    EntityLookupStatus,
    NonRetryableProviderError,
    PollStatus,
    RetryableProviderError,
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


def test_wishart_entity_404_is_miss():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    provider = _wishart_with(handler)
    result = provider.lookup_entity(IK_A)
    assert result.status == EntityLookupStatus.MISS


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
