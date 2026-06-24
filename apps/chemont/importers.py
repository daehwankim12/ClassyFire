"""ChemOnt ontology importer.

Reads a TSV file (chemont_dictionary.tsv) into :class:`ChemOntNode`, validating
parent reference integrity (every parent_id must exist in the file or DB).

Supported TSV columns (header required):
    chemont_id  name  parent_id  ontology_version
    numeric_id  chemont_id  name  parent_numeric_id  parent_name

Parent IDs are resolved in a two-pass approach:
  1. Insert all nodes with parent=NULL.
  2. Update parent FK for all nodes that declared a parent.
"""
from __future__ import annotations

import csv
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import TypedDict

from django.db import transaction

from apps.chemont.models import ChemOntNode

logger = logging.getLogger(__name__)

# Expected columns in supported TSV files.
LEGACY_COLUMNS = {"chemont_id", "name", "parent_id", "ontology_version"}
ZENODO_V3_COLUMNS = {
    "numeric_id",
    "chemont_id",
    "name",
    "parent_numeric_id",
    "parent_name",
}


def _parse_numeric_id(chemont_id: str) -> int:
    """Extract the integer part of a CHEMONT id string.

    e.g. ``"CHEMONT:0000001"`` -> ``1``.
    """
    try:
        return int(chemont_id.split(":")[1])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"Cannot parse numeric id from {chemont_id!r}") from exc


class ChemOntImportError(Exception):
    """Raised when the TSV file has structural or integrity problems."""


class NormalizedChemOntRow(TypedDict):
    numeric_id: int
    chemont_id: str
    name: str
    parent_numeric_id: int | None
    ontology_version: str


def _is_null_parent(value: str) -> bool:
    return value.strip().lower() in {"", "null", "none", r"\n"}


def _normalize_rows(
    rows: list[dict[str, str]],
    fieldnames: Sequence[str],
    ontology_version: str | None,
) -> list[NormalizedChemOntRow]:
    fields = set(fieldnames)
    normalized: list[NormalizedChemOntRow] = []

    if LEGACY_COLUMNS <= fields:
        for row in rows:
            cid = row["chemont_id"].strip()
            parent_id = row.get("parent_id", "").strip()
            normalized.append(
                {
                    "numeric_id": _parse_numeric_id(cid),
                    "chemont_id": cid,
                    "name": row["name"].strip(),
                    "parent_numeric_id": (
                        None if _is_null_parent(parent_id) else _parse_numeric_id(parent_id)
                    ),
                    "ontology_version": ontology_version
                    or row["ontology_version"].strip(),
                }
            )
        return normalized

    if ZENODO_V3_COLUMNS <= fields:
        for row in rows:
            parent_numeric = row.get("parent_numeric_id", "").strip()
            normalized.append(
                {
                    "numeric_id": int(row["numeric_id"].strip()),
                    "chemont_id": row["chemont_id"].strip(),
                    "name": row["name"].strip(),
                    "parent_numeric_id": (
                        None if _is_null_parent(parent_numeric) else int(parent_numeric)
                    ),
                    "ontology_version": ontology_version or "zenodo-v3",
                }
            )
        return normalized

    missing_legacy = LEGACY_COLUMNS - fields
    missing_v3 = ZENODO_V3_COLUMNS - fields
    raise ChemOntImportError(
        "TSV missing supported ChemOnt columns: "
        f"legacy missing={missing_legacy}; zenodo_v3 missing={missing_v3}"
    )


def import_chemont(
    tsv_path: str | Path,
    *,
    ontology_version: str | None = None,
    replace: bool = False,
) -> dict[str, int]:
    """Load ChemOnt nodes from *tsv_path* into the database.

    Parameters
    ----------
    tsv_path:
        Path to the TSV file.
    ontology_version:
        Override the version column from the file.  Useful for testing.
    replace:
        If ``True``, delete all existing nodes before import (use with care).

    Returns
    -------
    dict with keys ``inserted``, ``updated``, ``skipped``, ``total``.
    """
    path = Path(tsv_path)
    if not path.exists():
        raise FileNotFoundError(f"ChemOnt TSV not found: {path}")

    rows: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        if reader.fieldnames is None:
            raise ChemOntImportError("TSV file has no header row.")
        rows = list(reader)

    if not rows:
        logger.warning("chemont TSV is empty; nothing to import.")
        return {"inserted": 0, "updated": 0, "skipped": 0, "total": 0}

    normalized_rows = _normalize_rows(rows, reader.fieldnames, ontology_version)

    # Build in-file id set for integrity check.
    file_ids: set[int] = {int(r["numeric_id"]) for r in normalized_rows}

    # Validate parent refs: every non-empty parent_id must be in file_ids.
    orphans: list[int] = []
    for row in normalized_rows:
        pid = row["parent_numeric_id"]
        if pid is not None and pid not in file_ids:
            # Also accept if it already exists in DB.
            orphans.append(pid)

    if orphans:
        # Check DB for existing parents.
        db_ids = set(
            ChemOntNode.objects.filter(numeric_id__in=orphans).values_list(
                "numeric_id", flat=True
            )
        )
        still_missing = set(orphans) - db_ids
        if still_missing:
            raise ChemOntImportError(
                f"Parent reference integrity violation: {still_missing}"
            )

    inserted = 0
    updated = 0
    skipped = 0

    with transaction.atomic():
        if replace:
            ChemOntNode.objects.all().delete()

        # Pass 1: upsert all nodes with parent=NULL.
        for row in normalized_rows:
            numeric = int(row["numeric_id"])

            _obj, created = ChemOntNode.objects.update_or_create(
                numeric_id=numeric,
                defaults={
                    "chemont_id": str(row["chemont_id"]),
                    "name": str(row["name"]),
                    "parent": None,
                    "ontology_version": str(row["ontology_version"]),
                },
            )
            if created:
                inserted += 1
            else:
                updated += 1

        # Pass 2: set parent FKs.
        for row in normalized_rows:
            parent_numeric = row["parent_numeric_id"]
            if parent_numeric is None:
                continue
            ChemOntNode.objects.filter(numeric_id=int(row["numeric_id"])).update(
                parent_id=int(parent_numeric)
            )

    total = len(normalized_rows)
    logger.info(
        "ChemOnt import complete: inserted=%d updated=%d skipped=%d total=%d",
        inserted,
        updated,
        skipped,
        total,
    )
    return {"inserted": inserted, "updated": updated, "skipped": skipped, "total": total}
