"""Wishart ClassyFire HTTP provider (the default production provider).

Endpoints (relative to ``WISHART_BASE_URL``):

* ``GET  /entities/{InChIKey}.json`` — cached entity classification.
* ``POST /queries`` — submit a STRUCTURE query (label / query_input / query_type).
* ``GET  /queries/{query_id}.json`` — poll a submitted query.

HTTP outcomes are classified into the provider taxonomy:

* ``2xx`` with a body         → FOUND / COMPLETE.
* entity ``404``              → MISS (not an error; just no cached entity).
* ``429`` / ``5xx`` / timeout / connection reset → RETRYABLE (honour
  ``Retry-After`` when present).
* other definite ``4xx``      → non-retryable input error.

NB: tests never exercise this class against the network — they use
:class:`apps.remote.mock.MockProvider`. A guarded live smoke test against the
real Wishart service is deferred/manual.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
from django.conf import settings

from apps.remote.mock import parse_wishart_payload, parse_wishart_results
from apps.remote.provider import (
    EntityLookupResult,
    EntityLookupStatus,
    NonRetryableProviderError,
    NormalizedClassification,
    PollResult,
    PollStatus,
    RetryableProviderError,
    SubmitResult,
)


def _parse_retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class WishartDirectProvider:
    """Talks to the real Wishart ClassyFire HTTP API via httpx."""

    def __init__(
        self, base_url: str | None = None, timeout: float | None = None
    ) -> None:
        self.base_url = (base_url or settings.WISHART_BASE_URL).rstrip("/")
        self.timeout = (
            timeout if timeout is not None else settings.REMOTE_TIMEOUT_SECONDS
        )

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout)

    # -- Provider protocol -------------------------------------------------

    def lookup_entity(self, inchikey: str) -> EntityLookupResult:
        url = f"{self.base_url}/entities/{inchikey}.json"
        try:
            with self._client() as client:
                resp = client.get(url)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return EntityLookupResult(
                status=EntityLookupStatus.RETRYABLE_ERROR,
                inchikey=inchikey,
                detail=str(exc),
            )
        if resp.status_code == 404:
            return EntityLookupResult(
                status=EntityLookupStatus.MISS, inchikey=inchikey
            )
        if resp.status_code == 429 or resp.status_code >= 500:
            return EntityLookupResult(
                status=EntityLookupStatus.RETRYABLE_ERROR,
                inchikey=inchikey,
                retry_after=_parse_retry_after(resp),
                detail=f"HTTP {resp.status_code}",
            )
        if resp.status_code >= 400:
            # Definite 4xx input error: surface as MISS (no entity, do not retry).
            return EntityLookupResult(
                status=EntityLookupStatus.MISS,
                inchikey=inchikey,
                detail=f"HTTP {resp.status_code}",
            )
        try:
            raw = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            return EntityLookupResult(
                status=EntityLookupStatus.RETRYABLE_ERROR,
                inchikey=inchikey,
                detail=f"malformed body: {exc}",
            )
        if not raw:
            return EntityLookupResult(
                status=EntityLookupStatus.MISS, inchikey=inchikey
            )
        return EntityLookupResult(
            status=EntityLookupStatus.FOUND, inchikey=inchikey, raw_json=raw
        )

    def submit_structures(
        self, structures: list[tuple[str, str]]
    ) -> SubmitResult:
        query_input = "\n".join(f"{label}\t{struct}" for label, struct in structures)
        payload = {
            "label": "classyfire-batch",
            "query_input": query_input,
            "query_type": "STRUCTURE",
        }
        url = f"{self.base_url}/queries"
        try:
            with self._client() as client:
                resp = client.post(url, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RetryableProviderError(str(exc)) from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RetryableProviderError(
                f"HTTP {resp.status_code}", retry_after=_parse_retry_after(resp)
            )
        if resp.status_code >= 400:
            raise NonRetryableProviderError(f"HTTP {resp.status_code}")
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise RetryableProviderError(f"malformed body: {exc}") from exc
        query_id = body.get("id")
        if query_id is None:
            raise RetryableProviderError("submit response missing query id")
        return SubmitResult(remote_query_id=str(query_id), raw_json=body)

    def poll_query(self, remote_query_id: str) -> PollResult:
        url = f"{self.base_url}/queries/{remote_query_id}.json"
        try:
            with self._client() as client:
                resp = client.get(url)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return PollResult(status=PollStatus.RETRYABLE_ERROR, detail=str(exc))
        if resp.status_code == 429 or resp.status_code >= 500:
            return PollResult(
                status=PollStatus.RETRYABLE_ERROR,
                retry_after=_parse_retry_after(resp),
                detail=f"HTTP {resp.status_code}",
            )
        if resp.status_code >= 400:
            return PollResult(
                status=PollStatus.RETRYABLE_ERROR, detail=f"HTTP {resp.status_code}"
            )
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            return PollResult(
                status=PollStatus.RETRYABLE_ERROR, detail=f"malformed body: {exc}"
            )
        status = str(body.get("classification_status", "")).lower()
        if status and status not in {"done", "complete", "completed"}:
            return PollResult(status=PollStatus.RUNNING, raw_json=body)
        return PollResult(status=PollStatus.COMPLETE, raw_json=body)

    def parse_result(self, raw_json: dict[str, Any]) -> NormalizedClassification:
        # Wishart query results wrap entities; unwrap the single-entity case.
        payload = raw_json
        entities = raw_json.get("entities")
        if isinstance(entities, list) and entities:
            payload = entities[0]
        return parse_wishart_payload(payload)

    def parse_results(
        self, raw_json: dict[str, Any]
    ) -> list[NormalizedClassification]:
        # A completed /queries response wraps one entity per submitted
        # structure; parse EVERY entity so every batched task can be resolved.
        return parse_wishart_results(raw_json)
