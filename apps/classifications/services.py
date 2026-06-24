from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from django.conf import settings
from django.utils.module_loading import import_string

from apps.classifications.backends import ClassificationLookupBackend
from apps.core.enums import SOURCE_PRIORITY, SourceType

INCHIKEY_RE = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
DEFAULT_LOOKUP_BACKEND = (
    "apps.classifications.backends.PostgresClassificationLookupBackend"
)
_lookup_backend: ClassificationLookupBackend | None = None
_lookup_backend_path: str | None = None


class LookupTier(StrEnum):
    LIVE = "LIVE"
    SEED = "SEED"


@dataclass(frozen=True)
class ChemOntLabel:
    chemont_id: str | None
    name: str | None


@dataclass(frozen=True)
class ClassificationLookupResult:
    inchikey: str
    kingdom: ChemOntLabel | None
    superclass: ChemOntLabel | None
    class_: ChemOntLabel | None
    subclass: ChemOntLabel | None
    direct_parent: ChemOntLabel | None
    source_type: str
    source_priority: int
    source_ref: str | None
    classification_version: str | None
    retrieved_at: datetime | None
    query_id: str | None
    tier: LookupTier
    result_id: int | None = None


def reset_lookup_backend_cache() -> None:
    """Clear the process-local lookup backend instance cache.

    Tests and runtime config reload paths use this after changing
    ``CLASSIFICATION_LOOKUP_BACKEND``.
    """
    global _lookup_backend, _lookup_backend_path
    _lookup_backend = None
    _lookup_backend_path = None


def get_lookup_backend() -> ClassificationLookupBackend:
    """Return the configured current-classification lookup backend."""
    global _lookup_backend, _lookup_backend_path
    backend_path = str(
        getattr(
            settings,
            "CLASSIFICATION_LOOKUP_BACKEND",
            DEFAULT_LOOKUP_BACKEND,
        )
    )
    if _lookup_backend is None or _lookup_backend_path != backend_path:
        backend_cls = import_string(backend_path)
        _lookup_backend = backend_cls()
        _lookup_backend_path = backend_path
    return _lookup_backend


def _label_snapshot(label: ChemOntLabel | None) -> dict[str, str | None] | None:
    if label is None:
        return None
    return {"chemont_id": label.chemont_id, "name": label.name}


def classification_lookup_snapshot(
    result: ClassificationLookupResult,
) -> dict[str, Any]:
    """Serialize a lookup result for pinning on a BatchItem."""
    retrieved_at = (
        result.retrieved_at.isoformat() if result.retrieved_at is not None else None
    )
    return {
        "inchikey": result.inchikey,
        "kingdom": _label_snapshot(result.kingdom),
        "superclass": _label_snapshot(result.superclass),
        "class": _label_snapshot(result.class_),
        "subclass": _label_snapshot(result.subclass),
        "direct_parent": _label_snapshot(result.direct_parent),
        "source_type": result.source_type,
        "source_priority": result.source_priority,
        "source_ref": result.source_ref,
        "classification_version": result.classification_version,
        "retrieved_at": retrieved_at,
        "query_id": result.query_id,
        "tier": result.tier.value,
        "result_id": result.result_id,
    }


def snapshot_current_classification(inchikey: str) -> dict[str, Any] | None:
    result = lookup_current_classification(inchikey)
    if result is None:
        return None
    return classification_lookup_snapshot(result)


def _stored_label(row: dict[str, Any], prefix: str) -> ChemOntLabel | None:
    stored_labels = row.get("labels")
    if isinstance(stored_labels, str):
        try:
            stored_labels = json.loads(stored_labels)
        except json.JSONDecodeError:
            return None
    if not isinstance(stored_labels, dict):
        return None
    stored = stored_labels.get(prefix)
    if not isinstance(stored, dict):
        return None
    stored_cid = stored.get("chemont_id")
    stored_name = stored.get("name")
    if stored_cid is None and stored_name is None:
        return None
    return ChemOntLabel(
        chemont_id=str(stored_cid) if stored_cid is not None else None,
        name=str(stored_name) if stored_name is not None else None,
    )


def _label(row: dict[str, Any], prefix: str) -> ChemOntLabel | None:
    cid = row.get(f"{prefix}_chemont_id")
    name = row.get(f"{prefix}_name")
    if cid is None and name is None:
        return _stored_label(row, prefix)
    return ChemOntLabel(chemont_id=cid, name=name)


def _live_row_to_result(row: dict[str, Any]) -> ClassificationLookupResult:
    return ClassificationLookupResult(
        inchikey=row["inchikey"],
        kingdom=_label(row, "kingdom"),
        superclass=_label(row, "superclass"),
        class_=_label(row, "class"),
        subclass=_label(row, "subclass"),
        direct_parent=_label(row, "direct_parent"),
        source_type=row["source_type"],
        source_priority=row["source_priority"],
        source_ref=row.get("source_ref"),
        classification_version=row.get("classification_version"),
        retrieved_at=row.get("retrieved_at"),
        query_id=row.get("remote_query_id"),
        tier=LookupTier.LIVE,
        result_id=row.get("result_id"),
    )


def _seed_row_to_result(row: dict[str, Any]) -> ClassificationLookupResult:
    return ClassificationLookupResult(
        inchikey=row["inchikey"],
        kingdom=_label(row, "kingdom"),
        superclass=_label(row, "superclass"),
        class_=_label(row, "class"),
        subclass=_label(row, "subclass"),
        direct_parent=_label(row, "direct_parent"),
        # Seed rows are served as the historical-aggregate tier; surface its
        # canonical priority and the seed source id as provenance so callers
        # (and a future Go/Rust reader) can audit where the result came from.
        source_type=SourceType.HISTORICAL_AGGREGATE.value,
        source_priority=SOURCE_PRIORITY[SourceType.HISTORICAL_AGGREGATE],
        source_ref=(
            str(row["seed_source_id"]) if row.get("seed_source_id") is not None else None
        ),
        classification_version=row.get("seed_version"),
        retrieved_at=None,
        query_id=None,
        tier=LookupTier.SEED,
    )


def lookup_current_classification(inchikey: str) -> ClassificationLookupResult | None:
    """Look up classification for an exact 27-char InChIKey.

    Returns None if the key is not a valid full InChIKey (P-02: connectivity-only
    14-char prefix matches are never auto-applied) or if no record exists.
    Live results take precedence; within live, highest source_priority wins,
    ties broken by newest retrieved_at.
    """
    if not INCHIKEY_RE.match(inchikey):
        return None

    return get_lookup_backend().lookup_one(inchikey)


def lookup_current_classifications(
    inchikeys: list[str],
) -> dict[str, ClassificationLookupResult]:
    """Bulk equivalent of :func:`lookup_current_classification`.

    Runs ONE ``WHERE inchikey IN (...)`` live query and ONE seed query (instead
    of the per-item N+1), then applies the SAME live-precedence rule per key:
    a live hit wins; otherwise the seed hit (if any) is used. Invalid / non-27
    keys are skipped. The returned dict has identical ``ClassificationLookupResult``
    values to calling the single-key function per inchikey.
    """
    valid = [k for k in dict.fromkeys(inchikeys) if INCHIKEY_RE.match(k)]
    if not valid:
        return {}

    return get_lookup_backend().lookup_many(valid)
