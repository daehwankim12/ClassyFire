"""Wishart ClassyFire HTTP provider (the default production provider).

Endpoints (relative to ``WISHART_BASE_URL``):

* ``GET  /entities/{InChIKey}.json`` — cached entity classification.
* ``POST /queries`` — submit a STRUCTURE query (label / query_input / query_type).
* ``GET  /queries/{query_id}.json`` — poll a submitted query.

HTTP outcomes are classified EXPLICITLY (F-10) via
:func:`apps.remote.provider.classify_http_status`:

* ``2xx`` with a body         → FOUND / COMPLETE.
* ``404``                     → NOT_FOUND (entity/query absent → MISS for entity).
* ``401`` / ``403``           → AUTH_FAILURE (stop retrying, surface to operator).
* ``408`` / ``425`` / ``429`` / ``5xx`` / timeout / connection reset → RETRYABLE
  (honour ``Retry-After``, supporting BOTH delta-seconds AND HTTP-date forms).
* other definite ``4xx``      → PERMANENT (non-retryable input error).
* unknown/unmapped status     → PROTOCOL ERROR (never silently COMPLETE).

A SINGLE ``httpx.Client`` is created per provider instance and reused across all
calls (connection pooling) instead of a per-request client.

NB: tests never exercise this class against the network — they use
:class:`apps.remote.mock.MockProvider` or an ``httpx.MockTransport``. A guarded
live smoke test against the real Wishart service is deferred/manual.
"""

from __future__ import annotations

import email.utils
import json
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from django.conf import settings

from apps.remote.contract import (
    ProviderContractError,
    QueryPollContractStatus,
    validate_entity_lookup_payload,
    validate_query_poll_payload,
    validate_query_submit_payload,
)
from apps.remote.mock import parse_wishart_payload, parse_wishart_results
from apps.remote.provider import (
    AmbiguousSubmissionError,
    AuthProviderError,
    EntityLookupResult,
    EntityLookupStatus,
    HttpOutcome,
    NonRetryableProviderError,
    NormalizedClassification,
    PollResult,
    PollStatus,
    ProtocolProviderError,
    RetryableProviderError,
    SubmitResult,
    classify_http_status,
)

# Response headers safe to persist verbatim (no auth/cookies/PII). Lower-cased.
SAFE_RESPONSE_HEADERS = {"content-type", "retry-after", "x-request-id"}


def _safe_headers(response: httpx.Response) -> dict[str, str]:
    """Filter response headers to the auditable allow-list (lower-cased keys)."""
    return {
        name.lower(): value
        for name, value in response.headers.items()
        if name.lower() in SAFE_RESPONSE_HEADERS
    }


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse a ``Retry-After`` header supporting BOTH forms (F-10).

    * delta-seconds: ``Retry-After: 120`` → ``120.0``.
    * HTTP-date:     ``Retry-After: Wed, 21 Oct 2026 07:28:00 GMT`` → seconds
      from now until that instant (clamped at >= 0).

    Returns ``None`` when the header is absent or unparseable.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    raw = raw.strip()
    try:
        return float(raw)
    except (TypeError, ValueError):
        pass
    # HTTP-date form.
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta = (parsed - datetime.now(UTC)).total_seconds()
    return max(delta, 0.0)


class WishartDirectProvider:
    """Talks to the real Wishart ClassyFire HTTP API via httpx.

    Reuses a single ``httpx.Client`` (connection pooling) across calls.
    """

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        self.base_url = (base_url or settings.WISHART_BASE_URL).rstrip("/")
        self.timeout = timeout if timeout is not None else settings.REMOTE_TIMEOUT_SECONDS
        self._http_client: httpx.Client | None = None

    def _client(self) -> httpx.Client:
        """Return the shared, lazily-created httpx.Client (connection pooling)."""
        if self._http_client is None:
            self._http_client = httpx.Client(timeout=self.timeout)
        return self._http_client

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    # -- Provider protocol -------------------------------------------------

    def lookup_entity(self, inchikey: str) -> EntityLookupResult:
        url = f"{self.base_url}/entities/{inchikey}.json"
        try:
            resp = self._client().get(url)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return EntityLookupResult(
                status=EntityLookupStatus.RETRYABLE_ERROR,
                inchikey=inchikey,
                detail=str(exc),
                raw_text=f"{exc.__class__.__name__}: {exc}",
            )
        headers = _safe_headers(resp)
        raw_text = resp.text
        outcome = classify_http_status(resp.status_code)
        if outcome is HttpOutcome.NOT_FOUND:
            # 404 is a GENUINE entity miss (key absent from the cache). Surface
            # NOT_FOUND so the worker may try to CALCULATE it via a structure
            # submit — distinct from a PERMANENT protocol error below.
            return EntityLookupResult(
                status=EntityLookupStatus.NOT_FOUND,
                inchikey=inchikey,
                http_status=resp.status_code,
                raw_text=raw_text,
                safe_headers=headers,
            )
        if outcome is HttpOutcome.AUTH_FAILURE:
            return EntityLookupResult(
                status=EntityLookupStatus.AUTH_FAILURE,
                inchikey=inchikey,
                http_status=resp.status_code,
                detail=f"HTTP {resp.status_code}",
                raw_text=raw_text,
                safe_headers=headers,
            )
        if outcome is HttpOutcome.RETRYABLE:
            return EntityLookupResult(
                status=EntityLookupStatus.RETRYABLE_ERROR,
                inchikey=inchikey,
                retry_after=_parse_retry_after(resp),
                http_status=resp.status_code,
                detail=f"HTTP {resp.status_code}",
                raw_text=raw_text,
                safe_headers=headers,
            )
        if outcome is HttpOutcome.PERMANENT:
            # Definite non-404 4xx input error (400/422/other): this is NOT an
            # entity miss. Surface PERMANENT_ERROR — a non-retryable, never-submit
            # outcome — so the worker records the attempt and NEVER submits a
            # calculation for a malformed request (distinct from a genuine 404
            # NOT_FOUND miss, and from an unmapped PROTOCOL_ERROR response).
            return EntityLookupResult(
                status=EntityLookupStatus.PERMANENT_ERROR,
                inchikey=inchikey,
                http_status=resp.status_code,
                detail=f"HTTP {resp.status_code}",
                raw_text=raw_text,
                safe_headers=headers,
            )
        if outcome is HttpOutcome.PROTOCOL_ERROR:
            return EntityLookupResult(
                status=EntityLookupStatus.PROTOCOL_ERROR,
                inchikey=inchikey,
                http_status=resp.status_code,
                detail=f"unmapped HTTP {resp.status_code}",
                raw_text=raw_text,
                safe_headers=headers,
            )
        # SUCCESS
        try:
            raw = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            # A 2xx whose body is not valid JSON is a PROTOCOL violation, never a
            # transient/retryable condition: the response is structurally broken,
            # so surface PROTOCOL_ERROR (the raw body is preserved for audit).
            return EntityLookupResult(
                status=EntityLookupStatus.PROTOCOL_ERROR,
                inchikey=inchikey,
                http_status=resp.status_code,
                detail=f"malformed body: {exc}",
                raw_text=raw_text,
                safe_headers=headers,
            )
        try:
            validate_entity_lookup_payload(raw)
        except ProviderContractError as exc:
            return EntityLookupResult(
                status=EntityLookupStatus.PROTOCOL_ERROR,
                inchikey=inchikey,
                raw_json=cast(dict[str, Any], raw),
                http_status=resp.status_code,
                detail=str(exc),
                raw_text=raw_text,
                safe_headers=headers,
            )
        return EntityLookupResult(
            status=EntityLookupStatus.FOUND,
            inchikey=inchikey,
            raw_json=raw,
            http_status=resp.status_code,
            raw_text=raw_text,
            safe_headers=headers,
        )

    def submit_structures(self, structures: list[tuple[str, str]]) -> SubmitResult:
        query_input = "\n".join(f"{label}\t{struct}" for label, struct in structures)
        payload = {
            "label": "classyfire-batch",
            "query_input": query_input,
            "query_type": "STRUCTURE",
        }
        url = f"{self.base_url}/queries"
        try:
            resp = self._client().post(url, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # The POST may have reached (and been accepted by) the provider even
            # though the local response was lost. Surface it as AMBIGUOUS so the
            # worker parks the intent UNKNOWN (key held) and NEVER auto-resubmits
            # the same payload — re-POSTing could duplicate the submission (F-06).
            raise AmbiguousSubmissionError(
                str(exc), raw_text=f"{exc.__class__.__name__}: {exc}"
            ) from exc
        headers = _safe_headers(resp)
        raw_text = resp.text
        outcome = classify_http_status(resp.status_code)
        if outcome is HttpOutcome.RETRYABLE:
            raise RetryableProviderError(
                f"HTTP {resp.status_code}",
                retry_after=_parse_retry_after(resp),
                raw_text=raw_text,
                safe_headers=headers,
                http_status=resp.status_code,
            )
        if outcome is HttpOutcome.AUTH_FAILURE:
            raise AuthProviderError(
                f"HTTP {resp.status_code}",
                raw_text=raw_text,
                safe_headers=headers,
                http_status=resp.status_code,
            )
        if outcome is HttpOutcome.PROTOCOL_ERROR:
            raise ProtocolProviderError(
                f"unmapped HTTP {resp.status_code}",
                raw_text=raw_text,
                safe_headers=headers,
                http_status=resp.status_code,
            )
        if outcome in (HttpOutcome.PERMANENT, HttpOutcome.NOT_FOUND):
            raise NonRetryableProviderError(
                f"HTTP {resp.status_code}",
                raw_text=raw_text,
                safe_headers=headers,
                http_status=resp.status_code,
            )
        # SUCCESS
        # A 2xx means the provider ACCEPTED the POST: a query was very likely
        # created upstream. Failing to read the query id LOCALLY (malformed body
        # or a broken query-id schema) is AMBIGUOUS ACCEPTANCE, not a clean
        # failure. Surface it as AmbiguousSubmissionError so the worker parks the
        # intent UNKNOWN (idempotency key held) and NEVER auto-resubmits the same
        # payload — re-POSTing could duplicate the upstream submission (F-06).
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise AmbiguousSubmissionError(
                f"malformed body: {exc}",
                raw_text=raw_text,
                safe_headers=headers,
                http_status=resp.status_code,
            ) from exc
        try:
            body, query_id = validate_query_submit_payload(body)
        except ProviderContractError as exc:
            raise AmbiguousSubmissionError(
                str(exc),
                raw_text=raw_text,
                safe_headers=headers,
                http_status=resp.status_code,
            ) from exc
        return SubmitResult(
            remote_query_id=query_id,
            raw_json=body,
            http_status=resp.status_code,
            raw_text=raw_text,
            safe_headers=headers,
        )

    def poll_query(self, remote_query_id: str) -> PollResult:
        url = f"{self.base_url}/queries/{remote_query_id}.json"
        try:
            resp = self._client().get(url)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return PollResult(
                status=PollStatus.RETRYABLE_ERROR,
                detail=str(exc),
                raw_text=f"{exc.__class__.__name__}: {exc}",
            )
        headers = _safe_headers(resp)
        raw_text = resp.text
        outcome = classify_http_status(resp.status_code)
        if outcome is HttpOutcome.RETRYABLE:
            return PollResult(
                status=PollStatus.RETRYABLE_ERROR,
                retry_after=_parse_retry_after(resp),
                http_status=resp.status_code,
                detail=f"HTTP {resp.status_code}",
                raw_text=raw_text,
                safe_headers=headers,
            )
        if outcome is HttpOutcome.AUTH_FAILURE:
            return PollResult(
                status=PollStatus.AUTH_FAILURE,
                http_status=resp.status_code,
                detail=f"HTTP {resp.status_code}",
                raw_text=raw_text,
                safe_headers=headers,
            )
        if outcome is HttpOutcome.NOT_FOUND:
            return PollResult(
                status=PollStatus.NOT_FOUND,
                http_status=resp.status_code,
                detail=f"HTTP {resp.status_code}",
                raw_text=raw_text,
                safe_headers=headers,
            )
        if outcome is HttpOutcome.PERMANENT:
            return PollResult(
                status=PollStatus.PROTOCOL_ERROR,
                http_status=resp.status_code,
                detail=f"HTTP {resp.status_code}",
                raw_text=raw_text,
                safe_headers=headers,
            )
        if outcome is HttpOutcome.PROTOCOL_ERROR:
            return PollResult(
                status=PollStatus.PROTOCOL_ERROR,
                http_status=resp.status_code,
                detail=f"unmapped HTTP {resp.status_code}",
                raw_text=raw_text,
                safe_headers=headers,
            )
        # SUCCESS
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            # A 2xx poll body that is not valid JSON is a PROTOCOL violation, not a
            # transient condition: never treat it as RUNNING/COMPLETE. Surface
            # PROTOCOL_ERROR with the raw body preserved for audit.
            return PollResult(
                status=PollStatus.PROTOCOL_ERROR,
                http_status=resp.status_code,
                detail=f"malformed body: {exc}",
                raw_text=raw_text,
                safe_headers=headers,
            )
        try:
            body, status = validate_query_poll_payload(body)
        except ProviderContractError as exc:
            return PollResult(
                status=PollStatus.PROTOCOL_ERROR,
                raw_json=cast(dict[str, Any], body),
                http_status=resp.status_code,
                detail=str(exc),
                raw_text=raw_text,
                safe_headers=headers,
            )
        if status is QueryPollContractStatus.RUNNING:
            return PollResult(
                status=PollStatus.RUNNING,
                raw_json=body,
                http_status=resp.status_code,
                raw_text=raw_text,
                safe_headers=headers,
            )
        return PollResult(
            status=PollStatus.COMPLETE,
            raw_json=body,
            http_status=resp.status_code,
            raw_text=raw_text,
            safe_headers=headers,
        )

    def parse_result(self, raw_json: dict[str, Any]) -> NormalizedClassification:
        # Wishart query results wrap entities; unwrap the single-entity case.
        payload = raw_json
        entities = raw_json.get("entities")
        if isinstance(entities, list) and entities:
            payload = entities[0]
        return parse_wishart_payload(payload)

    def parse_results(
        self, raw_json: dict[str, Any], *, max_entities: int | None = 25
    ) -> list[NormalizedClassification]:
        # A completed /queries response wraps one entity per submitted
        # structure; parse only the expected bounded set so surplus provider
        # entities are treated as malformed before normalization.
        return parse_wishart_results(raw_json, max_entities=max_entities)
