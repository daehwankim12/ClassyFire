"""Deterministic, configurable in-memory provider for tests.

``MockProvider`` simulates every outcome the real Wishart client must handle —
found / miss / 429 / 5xx / timeout / response-key mismatch / running-then-
complete — WITHOUT touching the network. Every test in the suite drives the
worker through this provider so the suite is fully deterministic and offline.

Configuration is data, not monkeypatching: seed ``entities`` for entity hits,
``submit_results``/``poll_script`` for the submit→poll lifecycle, and the
``*_error`` knobs to force retryable/non-retryable failures on the Nth call.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from apps.remote.provider import (
    ChemOntSlot,
    EntityLookupResult,
    EntityLookupStatus,
    NonRetryableProviderError,
    NormalizedClassification,
    PollResult,
    PollStatus,
    RetryableProviderError,
    SubmitResult,
)


def build_raw(
    inchikey: str,
    *,
    kingdom: str = "Organic compounds",
    superclass: str = "Benzenoids",
    klass: str = "Benzene and substituted derivatives",
    subclass: str | None = None,
    direct_parent: str = "Benzene and substituted derivatives",
    version: str = "2.1",
) -> dict[str, Any]:
    """Build a Wishart-shaped classification payload for ``inchikey``."""

    def node(name: str | None, cid: str) -> dict[str, Any] | None:
        if name is None:
            return None
        return {"name": name, "chemont_id": cid}

    return {
        "inchikey": f"InChIKey={inchikey}",
        "kingdom": node(kingdom, "CHEMONT:0000000"),
        "superclass": node(superclass, "CHEMONT:0002448"),
        "class": node(klass, "CHEMONT:0000131"),
        "subclass": node(subclass, "CHEMONT:0000135"),
        "direct_parent": node(direct_parent, "CHEMONT:0000131"),
        "classification_version": version,
        "alternative_parents": [],
    }


def build_query_response(
    entities: list[dict[str, Any]],
    *,
    status: str = "Done",
) -> dict[str, Any]:
    """Build a Wishart /queries response wrapping ``entities``.

    Mirrors the real completed-query shape so tests can simulate a single query
    that resolves MULTIPLE batched structures deterministically.
    """
    return {"classification_status": status, "entities": list(entities)}


@dataclass
class MockProvider:
    """A scriptable provider. All state is explicit and deterministic."""

    # inchikey -> raw_json (entity hit). Missing key => MISS.
    entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Sequence of submit query ids handed out (cycled).
    submit_query_ids: list[str] = field(default_factory=lambda: ["q-1"])
    # remote_query_id -> ordered list of poll outcomes; each item is either
    # "RUNNING" or a raw_json dict (COMPLETE). The list is consumed front-to-back;
    # the last element repeats once exhausted.
    poll_script: dict[str, list[Any]] = field(default_factory=dict)

    # Forced failures (counted across calls of each operation).
    entity_retryable_for: set[str] = field(default_factory=set)
    entity_protocol_error_for: set[str] = field(default_factory=set)
    entity_retry_after: float | None = None
    submit_retryable: bool = False
    submit_non_retryable: bool = False
    poll_retryable_for: set[str] = field(default_factory=set)
    poll_protocol_error_for: set[str] = field(default_factory=set)

    # Call counters (assertable by tests).
    entity_calls: list[str] = field(default_factory=list)
    submit_calls: list[list[tuple[str, str]]] = field(default_factory=list)
    poll_calls: list[str] = field(default_factory=list)
    parse_calls: int = 0

    _ids: itertools.cycle[str] = field(init=False, repr=False)
    _poll_cursor: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._ids = itertools.cycle(self.submit_query_ids or ["q-1"])

    # -- Provider protocol -------------------------------------------------

    def lookup_entity(self, inchikey: str) -> EntityLookupResult:
        self.entity_calls.append(inchikey)
        if inchikey in self.entity_retryable_for:
            return EntityLookupResult(
                status=EntityLookupStatus.RETRYABLE_ERROR,
                inchikey=inchikey,
                retry_after=self.entity_retry_after,
                detail="simulated retryable error",
            )
        if inchikey in self.entity_protocol_error_for:
            return EntityLookupResult(
                status=EntityLookupStatus.PROTOCOL_ERROR,
                inchikey=inchikey,
                raw_json={"inchikey": inchikey, "kingdom": "malformed"},
                detail="simulated malformed entity payload",
            )
        raw = self.entities.get(inchikey)
        if raw is None:
            return EntityLookupResult(
                status=EntityLookupStatus.MISS, inchikey=inchikey
            )
        return EntityLookupResult(
            status=EntityLookupStatus.FOUND, inchikey=inchikey, raw_json=raw
        )

    def submit_structures(
        self, structures: list[tuple[str, str]]
    ) -> SubmitResult:
        self.submit_calls.append(list(structures))
        if self.submit_non_retryable:
            raise NonRetryableProviderError("simulated invalid structure")
        if self.submit_retryable:
            raise RetryableProviderError("simulated submit 5xx")
        query_id = next(self._ids)
        return SubmitResult(remote_query_id=query_id, raw_json={"id": query_id})

    def poll_query(self, remote_query_id: str) -> PollResult:
        self.poll_calls.append(remote_query_id)
        if remote_query_id in self.poll_retryable_for:
            return PollResult(
                status=PollStatus.RETRYABLE_ERROR, detail="simulated poll 5xx"
            )
        if remote_query_id in self.poll_protocol_error_for:
            return PollResult(
                status=PollStatus.PROTOCOL_ERROR,
                raw_json={"classification_status": "Bogus", "entities": []},
                detail="simulated malformed query payload",
            )
        script = self.poll_script.get(remote_query_id)
        if not script:
            # No script: behave as immediately complete with a generic result.
            return PollResult(status=PollStatus.COMPLETE, raw_json={})
        idx = self._poll_cursor.get(remote_query_id, 0)
        step = script[min(idx, len(script) - 1)]
        self._poll_cursor[remote_query_id] = idx + 1
        if step == "RUNNING":
            return PollResult(status=PollStatus.RUNNING)
        return PollResult(status=PollStatus.COMPLETE, raw_json=step)

    def parse_result(self, raw_json: dict[str, Any]) -> NormalizedClassification:
        self.parse_calls += 1
        return parse_wishart_payload(raw_json)

    def parse_results(
        self, raw_json: dict[str, Any]
    ) -> list[NormalizedClassification]:
        self.parse_calls += 1
        return parse_wishart_results(raw_json)


def _slot(raw: dict[str, Any], key: str) -> ChemOntSlot | None:
    node = raw.get(key)
    if not node:
        return None
    return ChemOntSlot(chemont_id=node.get("chemont_id"), name=node.get("name"))


def _normalize_inchikey(raw_value: Any) -> str | None:
    if not isinstance(raw_value, str):
        return None
    return raw_value.replace("InChIKey=", "").strip() or None


def parse_wishart_payload(raw_json: dict[str, Any]) -> NormalizedClassification:
    """Parse a Wishart-shaped payload into a NormalizedClassification.

    Shared by both the mock and the real Wishart provider so result parsing is
    tested once and used identically in production.
    """
    return NormalizedClassification(
        inchikey=_normalize_inchikey(raw_json.get("inchikey")),
        kingdom=_slot(raw_json, "kingdom"),
        superclass=_slot(raw_json, "superclass"),
        class_=_slot(raw_json, "class"),
        subclass=_slot(raw_json, "subclass"),
        direct_parent=_slot(raw_json, "direct_parent"),
        classification_version=raw_json.get("classification_version"),
        raw_json=raw_json,
        alternative_parents=list(raw_json.get("alternative_parents") or []),
    )


def parse_wishart_results(
    raw_json: dict[str, Any],
) -> list[NormalizedClassification]:
    """Parse EVERY entity in a Wishart query response into a list.

    A completed /queries response wraps one classification per submitted
    structure under ``entities``; each is parsed independently so the worker can
    resolve every batched task by its own response InChIKey. A payload without an
    ``entities`` list is treated as a single unwrapped entity (returns one item),
    keeping the single-structure path working. An empty response yields ``[]``.
    """
    entities = raw_json.get("entities")
    if isinstance(entities, list):
        return [
            parse_wishart_payload(entity)
            for entity in entities
            if isinstance(entity, dict)
        ]
    if raw_json:
        return [parse_wishart_payload(raw_json)]
    return []
