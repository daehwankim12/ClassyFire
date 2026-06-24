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


class HttpOutcome(StrEnum):
    """Explicit classification of a provider HTTP response (F-10).

    Every HTTP status maps to exactly one outcome; an unrecognized/unmapped
    status is ``PROTOCOL_ERROR`` and is NEVER silently treated as success.

    * 404                      -> NOT_FOUND (entity/query absent)
    * 401/403                  -> AUTH_FAILURE (stop retrying; surface)
    * 408/425/429/5xx/network  -> RETRYABLE (backoff; honour Retry-After)
    * other 4xx                -> PERMANENT (permanent input failure)
    * 2xx                      -> SUCCESS
    * anything else            -> PROTOCOL_ERROR
    """

    SUCCESS = "SUCCESS"
    NOT_FOUND = "NOT_FOUND"
    AUTH_FAILURE = "AUTH_FAILURE"
    RETRYABLE = "RETRYABLE"
    PERMANENT = "PERMANENT"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"


# HTTP status codes that are RETRYABLE per the F-10 contract.
RETRYABLE_HTTP_STATUSES: frozenset[int] = frozenset({408, 425, 429})


def classify_http_status(status_code: int) -> HttpOutcome:
    """Map an HTTP status code to its explicit :class:`HttpOutcome`.

    Pure + language-agnostic so a Go/Rust port can reproduce the exact routing.
    An unknown status (e.g. 1xx, 3xx redirects, 6xx) is ``PROTOCOL_ERROR``.
    """
    if 200 <= status_code < 300:
        return HttpOutcome.SUCCESS
    if status_code == 404:
        return HttpOutcome.NOT_FOUND
    if status_code in (401, 403):
        return HttpOutcome.AUTH_FAILURE
    if status_code in RETRYABLE_HTTP_STATUSES or 500 <= status_code < 600:
        return HttpOutcome.RETRYABLE
    if 400 <= status_code < 500:
        return HttpOutcome.PERMANENT
    return HttpOutcome.PROTOCOL_ERROR


class EntityLookupStatus(StrEnum):
    """Outcome of an entity (cached classification) lookup."""

    FOUND = "FOUND"
    MISS = "MISS"
    RETRYABLE_ERROR = "RETRYABLE_ERROR"
    AUTH_FAILURE = "AUTH_FAILURE"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"


class PollStatus(StrEnum):
    """Outcome of polling a submitted query."""

    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    RETRYABLE_ERROR = "RETRYABLE_ERROR"
    AUTH_FAILURE = "AUTH_FAILURE"
    NOT_FOUND = "NOT_FOUND"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"


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
    http_status: int | None = None
    raw_text: str | None = None
    safe_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SubmitResult:
    """Result of :meth:`Provider.submit_structures`."""

    remote_query_id: str
    raw_json: dict[str, Any] | None = None
    http_status: int | None = None
    raw_text: str | None = None
    safe_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PollResult:
    """Result of :meth:`Provider.poll_query`."""

    status: PollStatus
    raw_json: dict[str, Any] | None = None
    retry_after: float | None = None
    detail: str | None = None
    http_status: int | None = None
    raw_text: str | None = None
    safe_headers: dict[str, str] = field(default_factory=dict)


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
    """Base class for provider failures.

    Carries the raw response BODY text + allow-listed headers + HTTP status so
    the worker can record a COMPLETE, reproducible audit row even when a submit
    failed (non-JSON / permanent-error bodies are not silently dropped).
    """

    def __init__(
        self,
        message: str,
        *,
        raw_text: str | None = None,
        safe_headers: dict[str, str] | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_text = raw_text
        self.safe_headers = safe_headers or {}
        self.http_status = http_status


class RetryableProviderError(ProviderError):
    """A transient failure the worker should retry with backoff.

    ``retry_after`` (seconds), when set, takes absolute precedence over the
    computed backoff (server ``429``/``Retry-After``).
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        raw_text: str | None = None,
        safe_headers: dict[str, str] | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(
            message,
            raw_text=raw_text,
            safe_headers=safe_headers,
            http_status=http_status,
        )
        self.retry_after = retry_after


class NonRetryableProviderError(ProviderError):
    """A permanent failure (invalid structure, definite 4xx input error)."""


class AuthProviderError(NonRetryableProviderError):
    """An auth failure (401/403): stop retrying and surface for an operator.

    A subclass of :class:`NonRetryableProviderError` so existing callers that
    route non-retryable submit failures to a terminal state still work, while
    callers that care can distinguish an auth failure (do not burn attempts).
    """


class ProtocolProviderError(NonRetryableProviderError):
    """An unmapped/unknown provider status — never treated as success.

    A subclass of :class:`NonRetryableProviderError` (terminal) so an
    unrecognized status is surfaced explicitly rather than silently classified
    as a successful submit.
    """


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
