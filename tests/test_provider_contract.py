"""Contract validation tests for successful remote provider payloads.

All Wishart tests use httpx.MockTransport and never call the live service.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from apps.remote.mock import build_raw
from apps.remote.provider import (
    EntityLookupStatus,
    PollStatus,
    ProtocolProviderError,
)

IK_A = "RDHQFKQIGNGIED-MRVPVSSYSA-N"
IK_B = "WQZGKKKJIJFFOK-GASJEMHNSA-N"


def _wishart_with_json(status_code: int, payload: dict[str, Any]):
    from apps.remote.wishart import WishartDirectProvider

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    provider = WishartDirectProvider(base_url="https://wishart.test")
    transport = httpx.MockTransport(handler)
    provider._client = lambda: httpx.Client(transport=transport)  # type: ignore[method-assign]
    return provider


def test_entity_lookup_success_requires_consumed_entity_schema():
    raw = {"kingdom": {"name": "Organic compounds", "chemont_id": "CHEMONT:0"}}

    result = _wishart_with_json(200, raw).lookup_entity(IK_A)

    assert result.status == EntityLookupStatus.PROTOCOL_ERROR
    assert result.status != EntityLookupStatus.FOUND
    assert result.raw_json == raw
    assert result.detail is not None
    assert "inchikey" in result.detail


def test_entity_lookup_success_rejects_malformed_consumed_slot():
    raw = build_raw(IK_A)
    raw["kingdom"] = "Organic compounds"

    result = _wishart_with_json(200, raw).lookup_entity(IK_A)

    assert result.status == EntityLookupStatus.PROTOCOL_ERROR
    assert result.status != EntityLookupStatus.FOUND
    assert result.raw_json == raw
    assert result.detail is not None
    assert "kingdom" in result.detail


def test_query_submit_success_requires_scalar_query_id():
    with pytest.raises(ProtocolProviderError, match="query id"):
        _wishart_with_json(201, {"id": []}).submit_structures([(IK_A, "CCO")])


def test_query_submit_success_requires_present_query_id():
    with pytest.raises(ProtocolProviderError, match="query id"):
        _wishart_with_json(201, {}).submit_structures([(IK_A, "CCO")])


def test_query_poll_success_rejects_unknown_classification_status():
    raw = {"classification_status": "Banana", "entities": [build_raw(IK_A)]}

    result = _wishart_with_json(200, raw).poll_query("q-unknown-status")

    assert result.status == PollStatus.PROTOCOL_ERROR
    assert result.status != PollStatus.COMPLETE
    assert result.raw_json == raw
    assert result.detail is not None
    assert "classification_status" in result.detail


def test_query_poll_success_requires_classification_status():
    raw = {"entities": [build_raw(IK_A)]}

    result = _wishart_with_json(200, raw).poll_query("q-missing-status")

    assert result.status == PollStatus.PROTOCOL_ERROR
    assert result.status != PollStatus.COMPLETE
    assert result.raw_json == raw
    assert result.detail is not None
    assert "classification_status" in result.detail


def test_query_poll_complete_requires_entities_list():
    raw = {"classification_status": "Done"}

    result = _wishart_with_json(200, raw).poll_query("q-missing-entities")

    assert result.status == PollStatus.PROTOCOL_ERROR
    assert result.status != PollStatus.COMPLETE
    assert result.raw_json == raw
    assert result.detail is not None
    assert "entities" in result.detail


def test_query_poll_complete_rejects_malformed_entity_payload():
    raw = {"classification_status": "Done", "entities": [{"inchikey": 123}]}

    result = _wishart_with_json(200, raw).poll_query("q-bad-entity")

    assert result.status == PollStatus.PROTOCOL_ERROR
    assert result.status != PollStatus.COMPLETE
    assert result.raw_json == raw
    assert result.detail is not None
    assert "inchikey" in result.detail


def test_valid_success_payloads_still_parse_and_preserve_raw_payloads():
    entity_raw = build_raw(IK_A)
    entity_result = _wishart_with_json(200, entity_raw).lookup_entity(IK_A)

    assert entity_result.status == EntityLookupStatus.FOUND
    assert entity_result.raw_json == entity_raw

    submit_raw = {"id": 7777}
    submit = _wishart_with_json(201, submit_raw).submit_structures([(IK_A, "CCO")])
    assert submit.remote_query_id == "7777"
    assert submit.raw_json == submit_raw

    poll_raw = {
        "classification_status": "Done",
        "entities": [build_raw(IK_A), build_raw(IK_B)],
    }
    provider = _wishart_with_json(200, poll_raw)
    poll = provider.poll_query("q-valid")
    assert poll.status == PollStatus.COMPLETE
    assert poll.raw_json == poll_raw
    assert [norm.inchikey for norm in provider.parse_results(poll.raw_json or {})] == [
        IK_A,
        IK_B,
    ]
