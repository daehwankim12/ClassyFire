"""Provenance-preserving official import path (F-20, SourceType.LAB_OFFICIAL_IMPORT).

A lab-official classification import is reproducible from its source artifact:
the raw bytes are checksummed and recorded as an immutable :class:`SourceArtifact`,
then one immutable :class:`ClassificationResult` event is emitted per record and
the live pointer is advanced via :func:`write_live_result`. The artifact +
checksum let any reader reconstruct exactly which input produced each event.

Pipeline
--------
1. STAGE    — read the raw artifact bytes (caller supplies path or bytes).
2. VALIDATE — parse the JSON payload into per-record normalized classifications;
   reject malformed input before anything is persisted.
3. RECORD   — INSERT one immutable SourceArtifact (sha256 + byte_size + metadata).
4. EMIT     — for each record, write_live_result(...) appends an immutable event
   linked to the artifact and (when applied) advances the live pointer.

The expected artifact JSON shape is a list of records::

    [
      {
        "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
        "kingdom": {"chemont_id": "CHEMONT:0000000", "name": "Organic compounds"},
        "superclass": {...}, "class": {...}, "subclass": {...},
        "direct_parent": {...},
        "classification_version": "lab-v1",
        "raw": {... arbitrary upstream provenance ...}
      },
      ...
    ]
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from apps.classifications.models import SourceArtifact
from apps.classifications.result_writer import WriteOutcome, write_live_result
from apps.core.enums import SourceType
from apps.remote.provider import ChemOntSlot, NormalizedClassification


class OfficialImportError(ValueError):
    """The official-import artifact failed validation."""


@dataclass(frozen=True)
class OfficialImportResult:
    """Outcome of an official import run."""

    artifact_id: int
    checksum: str
    byte_size: int
    record_count: int
    outcomes: list[WriteOutcome]


def _slot(value: object) -> ChemOntSlot | None:
    if not isinstance(value, dict):
        return None
    chemont_id = value.get("chemont_id")
    name = value.get("name")
    if chemont_id is None and name is None:
        return None
    return ChemOntSlot(
        chemont_id=str(chemont_id) if chemont_id is not None else None,
        name=str(name) if name is not None else None,
    )


def _checksum(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _validate(raw: bytes) -> list[NormalizedClassification]:
    """Parse + validate the artifact bytes into normalized records.

    Raises :class:`OfficialImportError` on any malformed input before persisting.
    """
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OfficialImportError(f"Artifact is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise OfficialImportError("Artifact must be a JSON list of records.")

    records: list[NormalizedClassification] = []
    for i, record in enumerate(payload):
        if not isinstance(record, dict):
            raise OfficialImportError(f"Record {i} is not an object.")
        inchikey = record.get("inchikey")
        if not isinstance(inchikey, str) or len(inchikey) != 27:
            raise OfficialImportError(
                f"Record {i} has a missing/invalid 27-char inchikey."
            )
        records.append(
            NormalizedClassification(
                inchikey=inchikey,
                kingdom=_slot(record.get("kingdom")),
                superclass=_slot(record.get("superclass")),
                class_=_slot(record.get("class")),
                subclass=_slot(record.get("subclass")),
                direct_parent=_slot(record.get("direct_parent")),
                classification_version=record.get("classification_version"),
                raw_json=record.get("raw") if isinstance(record.get("raw"), dict)
                else record,
                alternative_parents=record.get("alternative_parents") or [],
            )
        )
    return records


@transaction.atomic
def import_official_artifact(
    raw: bytes,
    *,
    name: str,
    uri: str | None = None,
    metadata: dict | None = None,
    retrieved_at: datetime | None = None,
) -> OfficialImportResult:
    """Run the full official-import pipeline for one artifact's bytes.

    STAGE → VALIDATE → RECORD artifact → EMIT immutable event(s) → advance live.
    Everything runs in one transaction so a validation/emit failure leaves no
    partial artifact behind. Returns the artifact id + per-record write outcomes.
    """
    records = _validate(raw)

    artifact = SourceArtifact.objects.create(
        source_type=SourceType.LAB_OFFICIAL_IMPORT,
        name=name,
        uri=uri,
        checksum=_checksum(raw),
        byte_size=len(raw),
        metadata=metadata,
    )

    retrieved_at = retrieved_at or timezone.now()
    outcomes: list[WriteOutcome] = []
    for record in records:
        assert record.inchikey is not None  # validated above
        outcome = write_live_result(
            record.inchikey,
            record,
            source_type=SourceType.LAB_OFFICIAL_IMPORT,
            source_ref=f"artifact:{artifact.pk}",
            retrieved_at=retrieved_at,
            source_artifact=artifact,
        )
        outcomes.append(outcome)

    return OfficialImportResult(
        artifact_id=artifact.pk,
        checksum=artifact.checksum,
        byte_size=artifact.byte_size,
        record_count=len(records),
        outcomes=outcomes,
    )


def import_official_file(
    path: str | Path,
    *,
    uri: str | None = None,
    metadata: dict | None = None,
) -> OfficialImportResult:
    """Import an official-classification artifact from a filesystem path."""
    p = Path(path)
    raw = p.read_bytes()
    return import_official_artifact(
        raw,
        name=p.name,
        uri=uri if uri is not None else p.as_uri(),
        metadata=metadata,
    )
