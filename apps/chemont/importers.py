"""ChemOnt ontology importer.

Reads a TSV file (chemont_dictionary.tsv) into :class:`ChemOntNode`, validating
parent reference integrity (every parent_id must exist in the file or DB).

TSV columns (header required):
    chemont_id  name  parent_id  ontology_version

Parent IDs are resolved in a two-pass approach:
  1. Insert all nodes with parent=NULL.
  2. Update parent FK for all nodes that declared a parent.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from django.db import transaction

from apps.chemont.models import ChemOntNode

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Expected columns in the TSV file.
REQUIRED_COLUMNS = {"chemont_id", "name", "parent_id", "ontology_version"}


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
        missing = REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            raise ChemOntImportError(f"TSV missing columns: {missing}")
        rows = list(reader)

    if not rows:
        logger.warning("chemont TSV is empty; nothing to import.")
        return {"inserted": 0, "updated": 0, "skipped": 0, "total": 0}

    # Build in-file id set for integrity check.
    file_ids: set[str] = {r["chemont_id"].strip() for r in rows}

    # Validate parent refs: every non-empty parent_id must be in file_ids.
    orphans: list[str] = []
    for row in rows:
        pid = row.get("parent_id", "").strip()
        if pid and pid not in file_ids:
            # Also accept if it already exists in DB.
            orphans.append(pid)

    if orphans:
        # Check DB for existing parents.
        db_ids = set(
            ChemOntNode.objects.filter(chemont_id__in=orphans).values_list(
                "chemont_id", flat=True
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
        id_map: dict[str, int] = {}  # chemont_id -> numeric_id
        for row in rows:
            cid = row["chemont_id"].strip()
            name = row["name"].strip()
            ver = ontology_version or row["ontology_version"].strip()
            numeric = _parse_numeric_id(cid)
            id_map[cid] = numeric

            _obj, created = ChemOntNode.objects.update_or_create(
                numeric_id=numeric,
                defaults={
                    "chemont_id": cid,
                    "name": name,
                    "parent": None,
                    "ontology_version": ver,
                },
            )
            if created:
                inserted += 1
            else:
                updated += 1

        # Pass 2: set parent FKs.
        for row in rows:
            pid = row.get("parent_id", "").strip()
            if not pid:
                continue
            cid = row["chemont_id"].strip()
            numeric = id_map[cid]
            parent_numeric = _parse_numeric_id(pid)
            ChemOntNode.objects.filter(numeric_id=numeric).update(
                parent_id=parent_numeric
            )

    total = len(rows)
    logger.info(
        "ChemOnt import complete: inserted=%d updated=%d skipped=%d total=%d",
        inserted,
        updated,
        skipped,
        total,
    )
    return {"inserted": inserted, "updated": updated, "skipped": skipped, "total": total}
