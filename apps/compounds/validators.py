"""Main normalization entry point for ClassyFire normalization service.

Public interface
----------------
NormalizedRow  — dataclass with fields:
    detected_type:    DetectedInputType
    inchikey:         str | None  (27-char standard InChIKey, or None)
    canonical_smiles: str | None
    inchi:            str | None
    original_input:   str
    external_id:      str | None
    error:            str | None  (set iff normalization failed)

normalize_row(
    raw: str,
    type_override: DetectedInputType | None = None,
    external_id: str | None = None,
) -> NormalizedRow

Contract (language-agnostic):
- Deterministic. No hidden global state. No DB access.
- type_override skips detection and forces the given type.
- On parse failure: error is set, inchikey/canonical_smiles/inchi are None.
  detected_type is set to the attempted type (or UNKNOWN if detection returned
  UNKNOWN).
- Never raises out of the batch — one bad row cannot fail others.
- Stereochemistry and isotopes are preserved (no stripping/normalization).
"""
from __future__ import annotations

from dataclasses import dataclass

from apps.core.enums import DetectedInputType

from .detection import detect_input_type
from .identifiers import compute_identifiers


@dataclass
class NormalizedRow:
    """Result of normalizing a single batch input row."""

    detected_type: DetectedInputType
    inchikey: str | None
    canonical_smiles: str | None
    inchi: str | None
    original_input: str
    external_id: str | None
    error: str | None


def normalize_row(
    raw: str,
    type_override: DetectedInputType | None = None,
    external_id: str | None = None,
) -> NormalizedRow:
    """Normalize a single raw input string into a :class:`NormalizedRow`.

    Parameters
    ----------
    raw:
        The raw input string (SMILES, InChI, InChIKey, or single SDF block).
    type_override:
        When provided, skip auto-detection and parse as this type.
    external_id:
        Optional caller-supplied identifier (e.g. from a CSV ``id`` column).
    """
    try:
        detected_type = type_override if type_override is not None else detect_input_type(raw)
        inchikey, canonical_smiles, inchi = compute_identifiers(raw, detected_type)

        if inchikey is None and detected_type not in (
            DetectedInputType.UNKNOWN,
            DetectedInputType.INCHIKEY,
        ):
            return NormalizedRow(
                detected_type=detected_type,
                inchikey=None,
                canonical_smiles=None,
                inchi=None,
                original_input=raw,
                external_id=external_id,
                error=f"RDKit could not parse input as {detected_type}",
            )

        return NormalizedRow(
            detected_type=detected_type,
            inchikey=inchikey,
            canonical_smiles=canonical_smiles,
            inchi=inchi,
            original_input=raw,
            external_id=external_id,
            error=None,
        )
    except Exception as exc:
        return NormalizedRow(
            detected_type=DetectedInputType.UNKNOWN,
            inchikey=None,
            canonical_smiles=None,
            inchi=None,
            original_input=raw,
            external_id=external_id,
            error=str(exc),
        )
