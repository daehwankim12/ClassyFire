"""Resolve a compound STRUCTURE (InChI/SMILES) from public, key-free databases.

When an InChIKey-only batch item misses the local caches AND Wishart's entity
endpoint it lands in NEED_STRUCTURE: there is no structure to submit for
calculation, so it would otherwise wait for the user to supply one. Instead we
fetch the structure from public, key-free chemical databases keyed on InChIKey,
VERIFY it re-hashes to the same InChIKey (RDKit, via normalize_row), and feed it
back into the normal calculation path
(apps.batches.services.submit_structure_for_item).

Provider chain (ordered, all key-free), each rate-limited + circuit-guarded.
Both endpoints below were confirmed against the live services:
  * pubchem — PUG REST: GET /compound/inchikey/{ik}/property/InChI/JSON
              → ``PropertyTable.Properties[0].InChI``
  * cactus  — NCI/CADD resolver: GET /chemical/structure/{ik}/smiles
              → ``text/plain`` SMILES, HTTP 404 on a miss, 500 on error

Adding another provider (e.g. UniChem) is a one-line addition to ``_FETCHERS``
plus its rate in ``_PROVIDER_RATE_PER_MIN``. UniChem is intentionally left out
for now: its v1 ``/compounds`` is a POST-only cross-reference whose structure
payload we could not verify, and its coverage largely overlaps PubChem.

Politeness / rate limits (the design point):
  * Each provider draws from its OWN shared DB token bucket with the burst pinned
    to 1 (smooth pacing, no spike) at a conservative per-provider rate well under
    each service's known limits — PubChem documents 5 req/s & 400 req/min, so
    60/min (~1/s) is gentle; CACTUS publishes no limit (small NIH service) → 30/min.
  * A MISS (HTTP 404 / not indexed) is NOT a failure: the service answered, just
    had no data, so the per-provider circuit stays closed. Only transport errors,
    5xx, or 429 trip the circuit, so a flaky / throttling service is skipped — not
    hammered.
  * A bounded response read (1 MB) defends against unexpectedly large bodies.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx
from django.conf import settings
from django.core.cache import cache

from apps.compounds.inchikey import is_standard_inchikey, same_inchikey_skeleton
from apps.remote.circuit import DbCircuitBreaker
from apps.remote.rate_limit import DbTokenBucket

logger = logging.getLogger(__name__)

# Conservative per-provider request rates (req/min), spent SMOOTHLY (burst=1).
_PROVIDER_RATE_PER_MIN = {"pubchem": 60, "cactus": 30}
_DEFAULT_RATE_PER_MIN = 30
_MAX_RESPONSE_BYTES = 1_048_576


@dataclass(frozen=True)
class ResolvedStructure:
    """A structure string (InChI/SMILES) plus the provider that supplied it."""

    structure: str
    source: str


class _TransientError(Exception):
    """A transport / 5xx / 429 failure that should trip the provider circuit."""


def _timeout() -> float:
    return float(getattr(settings, "STRUCTURE_RESOLVE_TIMEOUT_SECONDS", 15))


def _request(
    method: str, url: str, *, json_body: dict | None = None
) -> tuple[int, str] | None:
    """HTTP request with a hard response-body cap.

    Returns ``(status_code, body_text)`` or ``None`` on transport error/timeout.
    The SINGLE network chokepoint for this module — tests patch this.
    """
    try:
        with httpx.Client(timeout=_timeout()) as client:
            with client.stream(method, url, json=json_body) as resp:
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) >= _MAX_RESPONSE_BYTES:
                        break
                text = bytes(body[:_MAX_RESPONSE_BYTES]).decode("utf-8", "replace")
                return resp.status_code, text
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        logger.warning("structure resolve transport error (%s %s): %s", method, url, exc)
        return None


def _pubchem_fetch(inchikey: str) -> str | None:
    url = (
        "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/inchikey/"
        f"{inchikey}/property/InChI/JSON"
    )
    res = _request("GET", url)
    if res is None:
        raise _TransientError("transport")
    status, body = res
    if status == 404:
        return None  # genuine miss — the service is up
    if status == 429 or status >= 500:
        raise _TransientError(f"HTTP {status}")
    if status != 200:
        return None
    try:
        props = json.loads(body)["PropertyTable"]["Properties"]
        inchi = props[0].get("InChI") if props else None
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None
    return inchi or None


def _cactus_fetch(inchikey: str) -> str | None:
    url = f"https://cactus.nci.nih.gov/chemical/structure/{inchikey}/smiles"
    res = _request("GET", url)
    if res is None:
        raise _TransientError("transport")
    status, body = res
    if status == 404:
        return None  # genuine miss
    if status == 429 or status >= 500:
        raise _TransientError(f"HTTP {status}")
    if status != 200:
        return None
    text = body.strip()
    if not text:
        return None
    smiles = text.splitlines()[0].strip()
    # CACTUS can occasionally serve an HTML error body with HTTP 200; reject
    # anything that is empty or clearly not a structure line.
    if not smiles or smiles[0] == "<":
        return None
    return smiles


# name → raw fetcher (returns a structure string, None on a clean miss, or raises
# _TransientError on a failure that should trip the provider circuit).
_FETCHERS = {
    "pubchem": _pubchem_fetch,
    "cactus": _cactus_fetch,
}


def _miss_cache_key(inchikey: str) -> str:
    return f"structresolve:miss:{inchikey}"


def _key_matches(requested: str, computed: str | None) -> bool:
    """True iff a resolved structure's recomputed (standard) InChIKey corresponds
    to the requested key.

    Exact match for a standard requested key. A NON-STANDARD requested key can
    never equal the structure's recomputed STANDARD key, so accept it on a
    connectivity-skeleton (first-block) match — the structure is authoritative
    and submit_structure_for_item then re-indexes the item under the standard key.
    """
    if not computed:
        return False
    if computed == requested:
        return True
    return not is_standard_inchikey(requested) and same_inchikey_skeleton(
        requested, computed
    )


def _fetch(provider: str, inchikey: str) -> tuple[str, str | None]:
    """One rate-limited, circuit-guarded fetch from a single provider.

    Returns ``(outcome, structure)`` where outcome is:
      * ``"hit"``  — a structure string was returned (``structure`` set),
      * ``"miss"`` — the provider answered definitively with no usable data,
      * ``"skip"`` — rate limited / circuit open / transport failure (NOT a
                     definitive answer; the key may still resolve later).

    The rate bucket is checked BEFORE the circuit so a rate-limit skip never
    burns a half-open circuit probe. Only failures (not clean misses) trip the
    circuit.
    """
    rate = _PROVIDER_RATE_PER_MIN.get(provider, _DEFAULT_RATE_PER_MIN)
    bucket = DbTokenBucket(
        "resolve", capacity=1, refill_per_min=rate, provider=f"structure:{provider}"
    )
    if not bucket.allow():
        return ("skip", None)  # rate limited → retry next sweep
    circuit = DbCircuitBreaker.from_settings(provider=f"structure:{provider}")
    if not circuit.allow():
        return ("skip", None)  # circuit open → skip without calling out
    try:
        structure = _FETCHERS[provider](inchikey)
    except _TransientError as exc:
        circuit.record_failure()
        logger.warning(
            "structure resolve provider %s failed for %s: %s", provider, inchikey, exc
        )
        return ("skip", None)
    circuit.record_success()  # answered (hit OR clean miss) → circuit healthy
    return ("hit", structure) if structure else ("miss", None)


def resolve_structure(inchikey: str) -> ResolvedStructure | None:
    """Try the provider chain; return the first structure that re-hashes to
    ``inchikey``, else ``None``. Disabled via settings → always ``None``.

    Verification matters: a provider may return a structure whose standardized
    InChIKey differs (tautomer/stereo/charge standardization), so we recompute
    the InChIKey (normalize_row) and only accept an exact match — otherwise we
    move on to the next provider. submit_structure_for_item re-checks this too.

    Negative caching: a key that EVERY provider definitively missed (none merely
    skipped for rate/circuit reasons) is cached for ``STRUCTURE_RESOLVE_MISS_TTL_
    SECONDS`` so a truly-novel compound — absent from every public DB — is not
    re-queried on every sweep. A rate-limited/transport skip is NOT cached, so a
    key only temporarily un-fetchable is retried promptly.
    """
    if not getattr(settings, "STRUCTURE_RESOLVE_ENABLED", False):
        return None
    miss_key = _miss_cache_key(inchikey)
    if cache.get(miss_key):
        return None
    # Local import: RDKit normalization lives in apps.compounds (a lower layer).
    from apps.compounds.validators import normalize_row

    any_skipped = False
    for provider in getattr(settings, "STRUCTURE_RESOLVE_PROVIDERS", ()):
        provider = provider.strip().lower()
        if provider not in _FETCHERS:
            continue
        outcome, structure = _fetch(provider, inchikey)
        if outcome == "skip":
            any_skipped = True
            continue
        if outcome == "miss" or structure is None:
            continue
        norm = normalize_row(structure)
        if norm.error is None and _key_matches(inchikey, norm.inchikey):
            return ResolvedStructure(structure=structure, source=provider)
        # Provider HAD data but it re-hashes to a different key — a definitive
        # (not skipped) non-result for this provider; fall through to the next.
        logger.info(
            "structure resolve: %s returned a structure for %s that re-hashes to "
            "%s (mismatch) — trying next provider",
            provider,
            inchikey,
            norm.inchikey,
        )
    # No usable structure. Only negative-cache a DEFINITIVE all-provider miss so
    # transient (rate/circuit/transport) skips are retried, not suppressed.
    if not any_skipped:
        ttl = int(getattr(settings, "STRUCTURE_RESOLVE_MISS_TTL_SECONDS", 21600))
        if ttl > 0:
            cache.set(miss_key, True, timeout=ttl)
    return None
