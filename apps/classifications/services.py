from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from apps.classifications.selectors import get_live_classification, get_seed_classification

INCHIKEY_RE = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")


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


def _label(row: dict[str, Any], prefix: str) -> ChemOntLabel | None:
    cid = row.get(f"{prefix}_chemont_id")
    name = row.get(f"{prefix}_name")
    if cid is None and name is None:
        return None
    return ChemOntLabel(chemont_id=cid, name=name)


def lookup_current_classification(inchikey: str) -> ClassificationLookupResult | None:
    """Look up classification for an exact 27-char InChIKey.

    Returns None if the key is not a valid full InChIKey (P-02: connectivity-only
    14-char prefix matches are never auto-applied) or if no record exists.
    Live results take precedence; within live, highest source_priority wins,
    ties broken by newest retrieved_at.
    """
    if not INCHIKEY_RE.match(inchikey):
        return None

    row = get_live_classification(inchikey)
    if row is not None:
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
        )

    row = get_seed_classification(inchikey)
    if row is not None:
        return ClassificationLookupResult(
            inchikey=row["inchikey"],
            kingdom=_label(row, "kingdom"),
            superclass=_label(row, "superclass"),
            class_=_label(row, "class"),
            subclass=_label(row, "subclass"),
            direct_parent=_label(row, "direct_parent"),
            source_type="SEED",
            source_priority=0,
            source_ref=None,
            classification_version=row.get("seed_version"),
            retrieved_at=None,
            query_id=None,
            tier=LookupTier.SEED,
        )

    return None
