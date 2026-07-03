"""Import Wishart ClassyFire web CSV exports as verified live results.

The ClassyFire web UI can sometimes return CSV results for structures whose
cache-server API path keeps failing. These CSV files are still Wishart
ClassyFire outputs, not operator-authored classifications. This importer keeps
the raw CSV as a source artifact, validates every web CompoundID against the
local submit mapping, then writes through the normal append-only live writer.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.batches.storage import get_artifact_storage
from apps.classifications.models import SourceArtifact
from apps.classifications.result_writer import WriteOutcome, write_live_result
from apps.core.enums import SourceType
from apps.remote.negative_cache import mark_negative_resolved_from_wishart
from apps.remote.provider import ChemOntSlot, NormalizedClassification

FULL_INCHIKEY_RE = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
WEB_CSV_PROVIDER = "wishart-classyfire-web-csv"

_REQUIRED_COLUMNS = ("CompoundID", "ChemOntID", "ClassifiedResults")
_SLOT_FIELDS = {
    "kingdom": "kingdom",
    "superclass": "superclass",
    "class": "class_",
    "subclass": "subclass",
    "direct_parent": "direct_parent",
}
_LIST_FIELDS = {
    "substituents",
    "external_descriptors",
    "ancestors",
    "predicted_chebi_terms",
    "predicted_lipidmaps_terms",
}


class ClassyfireWebCsvImportError(ValueError):
    """The ClassyFire web CSV artifact failed validation."""


@dataclass(frozen=True)
class ClassyfireWebCsvImportResult:
    artifact_id: int
    checksum: str
    byte_size: int
    record_count: int
    outcomes: list[WriteOutcome]


def _checksum(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ClassyfireWebCsvImportError(f"CSV is not valid UTF-8: {exc}") from exc


def _clean_value(value: str) -> str | None:
    cleaned = value.strip()
    if cleaned in ("", "''", '""'):
        return None
    return cleaned


def _normalize_field(value: str) -> str:
    return value.strip().lower()


def _normalize_chemont_id(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned or cleaned == "NotAvailable":
        return None
    if cleaned.startswith("CHEMONTID:"):
        return f"CHEMONT:{cleaned.removeprefix('CHEMONTID:')}"
    if cleaned.startswith("CHEMONT:"):
        return cleaned
    return None


def _slot(row: dict[str, str], value: str | None) -> ChemOntSlot | None:
    chemont_id = _normalize_chemont_id(row.get("ChemOntID"))
    if chemont_id is None and value is None:
        return None
    return ChemOntSlot(chemont_id=chemont_id, name=value)


def _parse_result_cell(cell: str, *, row_number: int) -> tuple[str, str | None]:
    if ":" not in cell:
        raise ClassyfireWebCsvImportError(
            f"CSV row {row_number} has malformed ClassifiedResults."
        )
    raw_field, raw_value = cell.split(":", 1)
    field = _normalize_field(raw_field)
    value = _clean_value(raw_value)
    if not field:
        raise ClassyfireWebCsvImportError(
            f"CSV row {row_number} has empty ClassifiedResults field."
        )
    return field, value


def _parse_json_list(value: str | None, *, field: str, compound_id: str) -> list[Any]:
    if value is None:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ClassyfireWebCsvImportError(
            f"{compound_id} has invalid JSON list for {field}: {exc}"
        ) from exc
    if not isinstance(parsed, list):
        raise ClassyfireWebCsvImportError(
            f"{compound_id} has non-list value for {field}."
        )
    return parsed


def _validate_mapping(compound_id: str, mapping: dict[str, str]) -> str:
    inchikey = mapping.get(compound_id)
    if not inchikey:
        raise ClassyfireWebCsvImportError(
            f"CompoundID {compound_id!r} has no InChIKey mapping."
        )
    if FULL_INCHIKEY_RE.fullmatch(inchikey) is None:
        raise ClassyfireWebCsvImportError(
            f"CompoundID {compound_id!r} maps to an invalid full InChIKey."
        )
    return inchikey


def _read_csv_rows(raw: bytes) -> list[dict[str, str]]:
    text = _decode(raw)
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ClassyfireWebCsvImportError("CSV is empty.")
    missing = [col for col in _REQUIRED_COLUMNS if col not in reader.fieldnames]
    if missing:
        raise ClassyfireWebCsvImportError(
            f"CSV missing required column(s): {', '.join(missing)}."
        )
    rows: list[dict[str, str]] = []
    for row_number, row in enumerate(reader, start=2):
        normalized = {key: (value or "") for key, value in row.items() if key is not None}
        if not any(value.strip() for value in normalized.values()):
            continue
        for col in _REQUIRED_COLUMNS:
            if not normalized.get(col, "").strip():
                raise ClassyfireWebCsvImportError(
                    f"CSV row {row_number} is missing {col}."
                )
        normalized["_row_number"] = str(row_number)
        rows.append(normalized)
    if not rows:
        raise ClassyfireWebCsvImportError("CSV contains no result rows.")
    return rows


def _group_rows(
    rows: list[dict[str, str]], identifier_to_inchikey: dict[str, str]
) -> dict[str, tuple[str, list[dict[str, str]]]]:
    grouped: dict[str, tuple[str, list[dict[str, str]]]] = {}
    for row in rows:
        compound_id = row["CompoundID"].strip()
        inchikey = _validate_mapping(compound_id, identifier_to_inchikey)
        if compound_id not in grouped:
            grouped[compound_id] = (inchikey, [])
        elif grouped[compound_id][0] != inchikey:
            raise ClassyfireWebCsvImportError(
                f"CompoundID {compound_id!r} maps to multiple InChIKeys."
            )
        grouped[compound_id][1].append(row)
    return grouped


def _build_record(
    compound_id: str,
    inchikey: str,
    rows: list[dict[str, str]],
) -> NormalizedClassification:
    slots: dict[str, ChemOntSlot | None] = dict.fromkeys(_SLOT_FIELDS.values())
    alternative_parents: list[dict[str, str | None]] = []
    extras: dict[str, Any] = {}
    classification_version: str | None = None
    normalized_rows: list[dict[str, str]] = []

    for row in rows:
        field, value = _parse_result_cell(
            row["ClassifiedResults"], row_number=int(row["_row_number"])
        )
        row_without_internal = {key: value for key, value in row.items() if key != "_row_number"}
        normalized_rows.append(row_without_internal)
        if field in _SLOT_FIELDS:
            slots[_SLOT_FIELDS[field]] = _slot(row, value)
            continue
        if field == "alternative_parents":
            slot = _slot(row, value)
            if slot is not None:
                alternative_parents.append(
                    {"chemont_id": slot.chemont_id, "name": slot.name}
                )
            continue
        if field == "classification_version":
            classification_version = value
            continue
        if field == "mmlecular_framework":
            extras["molecular_framework"] = value
            continue
        if field in _LIST_FIELDS:
            extras[field] = _parse_json_list(value, field=field, compound_id=compound_id)
            continue
        extras[field] = value

    raw_json = {
        "source": WEB_CSV_PROVIDER,
        "compound_id": compound_id,
        "inchikey": inchikey,
        "classification_version": classification_version,
        "rows": normalized_rows,
        "kingdom": _slot_payload(slots["kingdom"]),
        "superclass": _slot_payload(slots["superclass"]),
        "class": _slot_payload(slots["class_"]),
        "subclass": _slot_payload(slots["subclass"]),
        "direct_parent": _slot_payload(slots["direct_parent"]),
        "alternative_parents": alternative_parents,
        **extras,
    }
    return NormalizedClassification(
        inchikey=inchikey,
        kingdom=slots["kingdom"],
        superclass=slots["superclass"],
        class_=slots["class_"],
        subclass=slots["subclass"],
        direct_parent=slots["direct_parent"],
        classification_version=classification_version,
        raw_json=raw_json,
        alternative_parents=alternative_parents,
    )


def _slot_payload(slot: ChemOntSlot | None) -> dict[str, str | None] | None:
    if slot is None:
        return None
    return {"chemont_id": slot.chemont_id, "name": slot.name}


def parse_classyfire_web_csv(
    raw: bytes, identifier_to_inchikey: dict[str, str]
) -> list[NormalizedClassification]:
    """Parse a ClassyFire web CSV export into normalized live-write records."""
    rows = _read_csv_rows(raw)
    grouped = _group_rows(rows, identifier_to_inchikey)
    return [
        _build_record(compound_id, inchikey, group_rows)
        for compound_id, (inchikey, group_rows) in grouped.items()
    ]


@transaction.atomic
def import_classyfire_web_csv_artifact(
    raw: bytes,
    *,
    identifier_to_inchikey: dict[str, str],
    name: str,
    uri: str | None = None,
    metadata: dict[str, Any] | None = None,
    retrieved_at: datetime | None = None,
) -> ClassyfireWebCsvImportResult:
    """Validate, persist, and apply one ClassyFire web CSV export."""
    records = parse_classyfire_web_csv(raw, identifier_to_inchikey)
    checksum = _checksum(raw)
    storage_key = f"{checksum}.csv"
    get_artifact_storage(root=settings.SOURCE_ARTIFACT_DIR).save_bytes(storage_key, raw)

    artifact_metadata = {
        "provider": WEB_CSV_PROVIDER,
        "identifier_count": len(identifier_to_inchikey),
    }
    if metadata:
        artifact_metadata.update(metadata)
    artifact = SourceArtifact.objects.create(
        source_type=SourceType.WISHART_LIVE_QUERY,
        name=name,
        uri=uri,
        checksum=checksum,
        content_sha256=checksum,
        storage_key=storage_key,
        byte_size=len(raw),
        metadata=artifact_metadata,
    )

    retrieved_at = retrieved_at or timezone.now()
    outcomes: list[WriteOutcome] = []
    for record in records:
        assert record.inchikey is not None
        outcome = write_live_result(
            record.inchikey,
            record,
            source_type=SourceType.WISHART_LIVE_QUERY,
            source_ref=f"classyfire-web-csv:{artifact.pk}",
            retrieved_at=retrieved_at,
            source_artifact=artifact,
        )
        outcomes.append(outcome)
        mark_negative_resolved_from_wishart(record.inchikey)

    return ClassyfireWebCsvImportResult(
        artifact_id=artifact.pk,
        checksum=artifact.checksum,
        byte_size=artifact.byte_size,
        record_count=len(records),
        outcomes=outcomes,
    )


def import_classyfire_web_csv_file(
    path: str | Path,
    *,
    identifier_to_inchikey: dict[str, str],
    uri: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ClassyfireWebCsvImportResult:
    p = Path(path)
    raw = p.read_bytes()
    return import_classyfire_web_csv_artifact(
        raw,
        identifier_to_inchikey=identifier_to_inchikey,
        name=p.name,
        uri=uri if uri is not None else p.resolve().as_uri(),
        metadata=metadata,
    )
