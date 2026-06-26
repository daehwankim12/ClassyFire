"""Provider response contract validation for the Wishart boundary.

These validators check only the fields the application consumes. They keep the
original payload objects intact so callers can persist raw provider responses.
"""
from __future__ import annotations

import re
from enum import StrEnum
from typing import Any


class ProviderContractError(ValueError):
    """A successful provider response violated the expected payload contract."""


class QueryPollContractStatus(StrEnum):
    """Validated query poll status values used by the provider boundary."""

    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"


_ENTITY_SLOT_KEYS = ("kingdom", "superclass", "class", "subclass", "direct_parent")
_INCHIKEY_RE = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
INCHIKEY_LENGTH = 27
MAX_STRING_LENGTH = 4096
MAX_ALTERNATIVE_PARENTS = 100
MAX_QUERY_ENTITIES = 1000
_COMPLETE_STATUSES = frozenset({"done", "complete", "completed"})
MAX_REMOTE_QUERY_ID_LENGTH = 512

_RUNNING_STATUSES = frozenset(
    {
        "in progress",
        "pending",
        "queued",
        "running",
        "submitted",
        "waiting",
    }
)


def _object(payload: Any, context: str) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    raise ProviderContractError(f"{context} must be a JSON object")


def _normalized_inchikey(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.replace("InChIKey=", "").strip()
    if not normalized or _INCHIKEY_RE.fullmatch(normalized) is None:
        return None
    return normalized


def _normalized_full_inchikey(value: Any) -> str | None:
    normalized = _normalized_inchikey(value)
    if normalized is None or len(normalized) != INCHIKEY_LENGTH:
        return None
    return normalized


def _status_token(value: Any) -> str:
    if not isinstance(value, str):
        raise ProviderContractError("classification_status must be a string")
    token = " ".join(value.strip().lower().split())
    if not token:
        raise ProviderContractError("classification_status must not be empty")
    return token


def _validate_string(value: Any, context: str) -> None:
    if value is not None and not isinstance(value, str):
        raise ProviderContractError(f"{context} must be a string or null")
    if isinstance(value, str) and len(value) > MAX_STRING_LENGTH:
        raise ProviderContractError(f"{context} exceeds {MAX_STRING_LENGTH} characters")


def _validate_slot(raw: dict[str, Any], key: str) -> None:
    if key not in raw:
        return
    value = raw[key]
    if value is None:
        return
    if not isinstance(value, dict):
        raise ProviderContractError(f"{key} must be an object or null")
    for field_name in ("chemont_id", "name"):
        _validate_string(value.get(field_name), f"{key}.{field_name}")


def validate_entity_lookup_payload(payload: Any) -> dict[str, Any]:
    """Validate a successful entity lookup response and return the same dict."""
    raw = _object(payload, "entity lookup response")
    if _normalized_full_inchikey(raw.get("inchikey")) is None:
        raise ProviderContractError("entity lookup response missing valid inchikey")
    for key in _ENTITY_SLOT_KEYS:
        _validate_slot(raw, key)
    _validate_string(raw.get("inchikey"), "inchikey")
    _validate_string(raw.get("classification_version"), "classification_version")
    alternative_parents = raw.get("alternative_parents")
    if alternative_parents is not None and not isinstance(alternative_parents, list):
        raise ProviderContractError("alternative_parents must be a list or null")
    if isinstance(alternative_parents, list) and len(alternative_parents) > MAX_ALTERNATIVE_PARENTS:
        raise ProviderContractError(
            f"alternative_parents exceeds {MAX_ALTERNATIVE_PARENTS} entries"
        )
    return raw


def validate_query_submit_payload(payload: Any) -> tuple[dict[str, Any], str]:
    """Validate a successful query submit response and return payload + query id."""
    raw = _object(payload, "query submit response")
    if "id" not in raw:
        raise ProviderContractError("query submit response missing query id")
    query_id = raw["id"]
    if isinstance(query_id, bool) or not isinstance(query_id, (int, str)):
        raise ProviderContractError("query id must be a string or integer")
    remote_query_id = str(query_id).strip()
    if not remote_query_id:
        raise ProviderContractError("query id must not be empty")
    if len(remote_query_id) > MAX_REMOTE_QUERY_ID_LENGTH:
        raise ProviderContractError(
            f"query id must be at most {MAX_REMOTE_QUERY_ID_LENGTH} characters"
        )
    return raw, remote_query_id


def validate_query_poll_payload(
    payload: Any,
) -> tuple[dict[str, Any], QueryPollContractStatus]:
    """Validate a successful query poll response and return payload + status."""
    raw = _object(payload, "query poll response")
    token = _status_token(raw.get("classification_status"))
    if token in _RUNNING_STATUSES:
        return raw, QueryPollContractStatus.RUNNING
    if token not in _COMPLETE_STATUSES:
        raise ProviderContractError(
            f"unknown classification_status {raw.get('classification_status')!r}"
        )

    entities = raw.get("entities")
    if not isinstance(entities, list):
        raise ProviderContractError("complete query poll response requires entities list")
    if len(entities) > MAX_QUERY_ENTITIES:
        raise ProviderContractError(f"entities exceeds {MAX_QUERY_ENTITIES} entries")
    for index, entity in enumerate(entities):
        try:
            validate_entity_lookup_payload(entity)
        except ProviderContractError as exc:
            raise ProviderContractError(f"entities[{index}]: {exc}") from exc
    return raw, QueryPollContractStatus.COMPLETE
