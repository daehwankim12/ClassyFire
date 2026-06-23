"""Swappable provider boundary for remote ClassyFire (Wishart) automation.

The provider is the only place that talks to the outside world. It is a
*pure boundary*: it performs HTTP (or, in tests, deterministic simulation),
classifies the outcome into a small set of typed results, and never touches the
database or the queue. This keeps the Wishart client trivially replaceable by a
Go/Rust microservice exposing the same four operations.

Provider contract (EXACT methods, see task brief):

* ``lookup_entity(inchikey) -> EntityLookupResult``
* ``submit_structures(structures) -> SubmitResult``
* ``poll_query(remote_query_id) -> PollResult``
* ``parse_result(raw_json) -> NormalizedClassification``

Outcome taxonomy
----------------
Every network-ish operation maps to one of a few statuses. ``RETRYABLE_ERROR``
covers transient conditions (timeout, connection reset, HTTP 429/5xx, transient
malformed responses); a non-retryable input error is surfaced as ``MISS`` for
entity lookups or as a dedicated status / raised ``NonRetryableProviderError``
for submit/poll, so the worker can route to a terminal state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class EntityLookupStatus(StrEnum):
    """Outcome of an entity (cached classification) lookup."""

    FOUND = "FOUND"
    MISS = "MISS"
    RETRYABLE_ERROR = "RETRYABLE_ERROR"


class PollStatus(StrEnum):
    """Outcome of polling a submitted query."""

    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    RETRYABLE_ERROR = "RETRYABLE_ERROR"


@dataclass(frozen=True)
class EntityLookupResult:
    """Result of :meth:`Provider.lookup_entity`.

    ``raw_json`` carries the upstream payload on ``FOUND``. ``retry_after``
    mirrors a server ``Retry-After`` header (seconds) when present so the rate
    limiter can honour it with absolute precedence.
    """

    status: EntityLookupStatus
    inchikey: str
    raw_json: dict[str, Any] | None = None
    retry_after: float | None = None
    detail: str | None = None


@dataclass(frozen=True)
class SubmitResult:
    """Result of :meth:`Provider.submit_structures`."""

    remote_query_id: str
    raw_json: dict[str, Any] | None = None


@dataclass(frozen=True)
class PollResult:
    """Result of :meth:`Provider.poll_query`."""

    status: PollStatus
    raw_json: dict[str, Any] | None = None
    retry_after: float | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ChemOntSlot:
    """A single five-tier ChemOnt label (id + human name)."""

    chemont_id: str | None
    name: str | None


@dataclass(frozen=True)
class NormalizedClassification:
    """Provider-agnostic, normalized five-tier classification + provenance.

    ``inchikey`` is the InChIKey *reported by the response*; the worker MUST
    compare it to the requested key (result-key validation) before applying.
    """

    inchikey: str | None
    kingdom: ChemOntSlot | None
    superclass: ChemOntSlot | None
    class_: ChemOntSlot | None
    subclass: ChemOntSlot | None
    direct_parent: ChemOntSlot | None
    classification_version: str | None = None
    raw_json: dict[str, Any] | None = None
    alternative_parents: list[dict[str, Any]] = field(default_factory=list)


class ProviderError(Exception):
    """Base class for provider failures."""


class RetryableProviderError(ProviderError):
    """A transient failure the worker should retry with backoff.

    ``retry_after`` (seconds), when set, takes absolute precedence over the
    computed backoff (server ``429``/``Retry-After``).
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class NonRetryableProviderError(ProviderError):
    """A permanent failure (invalid structure, definite 4xx input error)."""


@runtime_checkable
class Provider(Protocol):
    """The remote provider boundary. Implementations: Wishart HTTP, Mock."""

    def lookup_entity(self, inchikey: str) -> EntityLookupResult:
        """Look up a cached entity classification for ``inchikey``."""

    def submit_structures(
        self, structures: list[tuple[str, str]]
    ) -> SubmitResult:
        """Submit ``(label, structure)`` pairs as a STRUCTURE query."""

    def poll_query(self, remote_query_id: str) -> PollResult:
        """Poll a previously-submitted query for completion."""

    def parse_result(self, raw_json: dict[str, Any]) -> NormalizedClassification:
        """Normalize a raw upstream payload into five-tier labels + key."""

    def parse_results(
        self, raw_json: dict[str, Any]
    ) -> list[NormalizedClassification]:
        """Normalize EVERY entity in a query response into a list.

        A completed /queries response wraps one classification per submitted
        structure under ``entities``. This yields one
        :class:`NormalizedClassification` per entity (each carrying its own
        response InChIKey + per-entity raw JSON) so the worker can resolve every
        task batched into the query. For an unwrapped single-entity payload this
        returns a one-element list.
        """
