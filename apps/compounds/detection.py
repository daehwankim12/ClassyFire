"""Input type detection for ClassyFire normalization service.

Public interface
----------------
detect_input_type(raw: str) -> DetectedInputType

Contract (language-agnostic):
- Strips leading/trailing whitespace before matching.
- Full standard InChIKey: matches ``^[A-Z]{14}-[A-Z]{10}-[A-Z]$`` (27 chars).
- InChI: stripped string starts with ``InChI=``.
- Otherwise: treated as SMILES.
- Never raises; returns UNKNOWN only if raw is empty after stripping.
"""
from __future__ import annotations

import re

from apps.core.enums import DetectedInputType

_INCHIKEY_RE = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")


def detect_input_type(raw: str) -> DetectedInputType:
    """Return the detected type of a single raw input string."""
    stripped = raw.strip()
    if not stripped:
        return DetectedInputType.UNKNOWN
    if _INCHIKEY_RE.match(stripped):
        return DetectedInputType.INCHIKEY
    if stripped.startswith("InChI="):
        return DetectedInputType.INCHI
    return DetectedInputType.SMILES
