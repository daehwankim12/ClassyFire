"""Bounded streaming upload parse + format dispatch for the real upload routes.

Public interface
----------------
iter_upload_rows(file, *, filename=None, content_type=None) -> Iterator[dict]
    Dispatch on filename suffix AND content-type and yield parser-style row
    dicts (keys: raw, row_index[, external_id]) by reading the uploaded file as
    a BOUNDED STREAM — never ``file.read()`` of the whole upload. CSV/TSV are
    parsed incrementally over a decoded line stream; SDF via a streaming record
    splitter on ``$$$$`` with per-record and per-property byte caps; everything
    else falls back to the plain text/line parser.

detect_upload_format(filename, content_type) -> UploadFormat
    Pure suffix+content-type dispatch (no I/O), exposed for testing.

Design — how memory is bounded
------------------------------
The Django ``UploadedFile`` exposes ``.chunks()`` (bounded reads) and is itself
iterable line-by-line. We NEVER materialise the whole file:

* TXT/CSV/TSV: wrap the binary upload in a ``TextIOWrapper`` and iterate it line
  by line; ``csv.reader`` consumes that iterator one row at a time. Only the
  current row (capped at MAX_STRUCTURE_LEN by the downstream guard / row-length
  cap here) is ever held.
* SDF: a hand-rolled splitter accumulates lines until the ``$$$$`` delimiter,
  emitting one record at a time. A per-record byte cap rejects (skips, with an
  error row) a pathologically huge record instead of buffering it whole; a
  per-property cap truncates oversize property values rather than retaining
  them. The accumulator is reset after every record, so peak memory is bounded
  by one record (≤ cap), not the file.

The ``check_upload_size`` guard still runs on the actual streamed byte count in
the route layer; this module additionally enforces per-row / per-record caps on
the REAL bytes as they stream, never on trusted metadata.
"""
from __future__ import annotations

import csv
import enum
import io
from collections.abc import Iterator
from typing import IO

from apps.core.security import (
    MAX_SDF_PROPERTY_LEN,
    MAX_STRUCTURE_LEN,
)

# Per-SDF-record byte cap. A record larger than this is skipped (it cannot be a
# legitimate single small molecule) and surfaced as an error row so the user
# sees it as INVALID_INPUT rather than silently vanishing — and so a malicious
# multi-GB "record" never gets buffered.
MAX_SDF_RECORD_LEN: int = MAX_STRUCTURE_LEN

# A single line longer than this is itself a red flag (no legitimate SMILES /
# InChI / molfile line approaches it); truncate it to keep memory bounded.
MAX_LINE_LEN: int = MAX_STRUCTURE_LEN


class UploadFormat(enum.StrEnum):
    """Dispatched parse format for an uploaded file."""

    CSV = "CSV"
    TSV = "TSV"
    SDF = "SDF"
    TEXT = "TEXT"


# Suffix → format. Lowercase, leading dot stripped at lookup time.
_SUFFIX_FORMATS: dict[str, UploadFormat] = {
    "csv": UploadFormat.CSV,
    "tsv": UploadFormat.TSV,
    "tab": UploadFormat.TSV,
    "sdf": UploadFormat.SDF,
    "txt": UploadFormat.TEXT,
}

# Content-type → format. Used only when the suffix is unknown/ambiguous.
_CONTENT_TYPE_FORMATS: dict[str, UploadFormat] = {
    "text/csv": UploadFormat.CSV,
    "application/csv": UploadFormat.CSV,
    "text/tab-separated-values": UploadFormat.TSV,
    "text/tsv": UploadFormat.TSV,
    "chemical/x-mdl-sdfile": UploadFormat.SDF,
    "chemical/x-mdl-sdf": UploadFormat.SDF,
    "application/x-mdl-sdfile": UploadFormat.SDF,
}


def iter_combined_rows(
    text_rows: Iterator[dict],
    file: IO[bytes] | None,
    *,
    filename: str | None = None,
    content_type: str | None = None,
) -> Iterator[dict]:
    """Lazily chain text-input rows then file rows with continuous row_index.

    Used by the upload routes so the DEFERRED path streams rows straight into
    the staging insert without ever building the full list. The synchronous path
    wraps this in ``list(...)`` for the existing quota signature.
    """
    next_index = 0
    for row in text_rows:
        yield {**row, "row_index": next_index}
        next_index += 1
    if file is not None:
        for row in iter_upload_rows(
            file, filename=filename, content_type=content_type
        ):
            yield {**row, "row_index": next_index}
            next_index += 1


def detect_upload_format(
    filename: str | None,
    content_type: str | None,
) -> UploadFormat:
    """Dispatch on filename suffix AND content-type. Suffix wins when known."""
    if filename:
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        fmt = _SUFFIX_FORMATS.get(suffix)
        if fmt is not None:
            return fmt

    if content_type:
        # Strip any "; charset=..." parameter.
        base = content_type.split(";", 1)[0].strip().lower()
        fmt = _CONTENT_TYPE_FORMATS.get(base)
        if fmt is not None:
            return fmt

    return UploadFormat.TEXT


def iter_upload_rows(
    file: IO[bytes],
    *,
    filename: str | None = None,
    content_type: str | None = None,
) -> Iterator[dict]:
    """Yield parser-style row dicts from *file*, dispatched + streamed.

    The returned iterator is lazy: rows are produced as the upload streams, so
    the caller (ingest_rows) can persist them in bounded chunks without ever
    holding the whole file or the whole row list in memory.
    """
    fmt = detect_upload_format(filename, content_type)
    if fmt is UploadFormat.SDF:
        yield from _iter_sdf(file)
    elif fmt in (UploadFormat.CSV, UploadFormat.TSV):
        delimiter = "," if fmt is UploadFormat.CSV else "\t"
        yield from _iter_csv_tsv(file, delimiter)
    else:
        yield from _iter_text(file)


# ---------------------------------------------------------------------------
# Streaming text / CSV / TSV
# ---------------------------------------------------------------------------


def _text_stream(file: IO[bytes]) -> IO[str]:
    """Wrap a binary upload in a decoding text stream (errors replaced).

    ``UploadedFile`` is a binary file-like object; ``TextIOWrapper`` decodes it
    lazily as it is iterated, so no full-file decode happens.
    """
    return io.TextIOWrapper(file, encoding="utf-8", errors="replace", newline="")


def _iter_text(file: IO[bytes]) -> Iterator[dict]:
    """Plain-text fallback: one non-blank line → one row."""
    row_index = 0
    for line in _text_stream(file):
        stripped = line.strip()
        if not stripped:
            continue
        if len(stripped) > MAX_LINE_LEN:
            yield {
                "raw": stripped[:200],
                "row_index": row_index,
                "parse_error": "ROW_TOO_LONG",
                "overflow_length": len(stripped),
            }
            row_index += 1
            continue
        yield {"raw": stripped, "row_index": row_index}
        row_index += 1


def _iter_csv_tsv(file: IO[bytes], delimiter: str) -> Iterator[dict]:
    """Stream a CSV/TSV upload: first column is ``raw``; an ``id``-like column
    (if present) is ``external_id``. Header row is consumed once.

    ``csv.reader`` pulls one line at a time from the underlying text iterator,
    so memory stays bounded to a single row.
    """
    reader = csv.reader(_text_stream(file), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        return

    id_index = _find_id_column(header)
    smiles_index = _find_smiles_column(header)

    row_index = 0
    for fields in reader:
        if not fields:
            continue
        raw = fields[smiles_index].strip() if smiles_index < len(fields) else ""
        if not raw:
            continue
        if len(raw) > MAX_LINE_LEN:
            yield {
                "raw": raw[:200],
                "row_index": row_index,
                "parse_error": "ROW_TOO_LONG",
                "overflow_length": len(raw),
            }
            row_index += 1
            continue
        result: dict = {"raw": raw, "row_index": row_index}
        if id_index is not None and id_index < len(fields):
            ext = fields[id_index].strip()
            if ext:
                result["external_id"] = ext[:MAX_SDF_PROPERTY_LEN]
        yield result
        row_index += 1


_ID_HEADER_NAMES = {"id", "external_id", "name", "identifier", "cid", "compound_id"}
_SMILES_HEADER_NAMES = {"smiles", "structure", "inchi", "input"}


def _find_id_column(header: list[str]) -> int | None:
    for i, name in enumerate(header):
        if name.strip().lower() in _ID_HEADER_NAMES:
            return i
    return None


def _find_smiles_column(header: list[str]) -> int:
    for i, name in enumerate(header):
        if name.strip().lower() in _SMILES_HEADER_NAMES:
            return i
    # Default: the first column carries the structure.
    return 0


# ---------------------------------------------------------------------------
# Streaming SDF with per-record / per-property caps
# ---------------------------------------------------------------------------


def _iter_sdf(file: IO[bytes]) -> Iterator[dict]:
    """Stream an SDF upload, splitting on ``$$$$`` with bounded buffers.

    Only one record is buffered at a time. A record that exceeds
    MAX_SDF_RECORD_LEN bytes is NOT buffered whole — once the cap is crossed we
    stop accumulating, drain the rest of the record, and emit an error row so it
    surfaces as INVALID_INPUT downstream (provenance preserved as a bounded
    preview). Property values are extracted with a per-property cap.
    """
    text_stream = _text_stream(file)
    row_index = 0
    buffer: list[str] = []
    buffer_bytes = 0
    overflowed = False

    for line in text_stream:
        if line.rstrip("\r\n") == "$$$$":
            if buffer or overflowed:
                yield _build_sdf_row(buffer, row_index, overflowed)
                row_index += 1
                buffer = []
                buffer_bytes = 0
                overflowed = False
            continue

        if overflowed:
            # Still draining an over-cap record: do not accumulate.
            continue

        line_len = len(line)
        if line_len > MAX_LINE_LEN:
            line = line[:MAX_LINE_LEN]
            line_len = MAX_LINE_LEN
        if buffer_bytes + line_len > MAX_SDF_RECORD_LEN:
            overflowed = True
            continue
        buffer.append(line)
        buffer_bytes += line_len

    # Trailing record with no closing $$$$.
    if (buffer and any(ln.strip() for ln in buffer)) or overflowed:
        yield _build_sdf_row(buffer, row_index, overflowed)


def _build_sdf_row(buffer: list[str], row_index: int, overflowed: bool) -> dict:
    """Build one parser-style row dict from an accumulated SDF block."""
    block = "".join(buffer)
    if overflowed:
        return {
            "raw": block[:200],
            "row_index": row_index,
            "input_type": "SDF",
            "parse_error": "SDF_RECORD_TOO_LONG",
            "overflow_length": max(len(block), MAX_SDF_RECORD_LEN + 1),
        }

    result: dict = {"raw": block.rstrip("\r\n"), "row_index": row_index, "input_type": "SDF"}
    ext_id = _extract_sdf_external_id(block)
    if ext_id:
        result["external_id"] = ext_id
    return result


def _extract_sdf_external_id(block: str) -> str | None:
    """Extract a bounded external id from an SDF block's data fields.

    Scans after ``M  END`` for ``> <NAME>`` / value pairs, capping each captured
    value at MAX_SDF_PROPERTY_LEN. Prefers EXTERNAL_ID/ID; falls back to the
    molblock header name line. Never buffers an unbounded property value.
    """
    lines = block.splitlines()
    in_data = False
    current_prop: str | None = None
    props: dict[str, str] = {}
    header_name: str | None = None

    for idx, line in enumerate(lines):
        if idx == 0:
            name = line.strip()
            if name:
                header_name = name[:MAX_SDF_PROPERTY_LEN]
        stripped = line.strip()
        if not in_data:
            if stripped == "M  END":
                in_data = True
            continue
        if stripped.startswith(">") and "<" in stripped:
            start = stripped.index("<") + 1
            end = stripped.index(">", start)
            current_prop = stripped[start:end]
        elif current_prop is not None and stripped:
            props[current_prop] = stripped[:MAX_SDF_PROPERTY_LEN]
            current_prop = None

    ext = props.get("EXTERNAL_ID") or props.get("ID") or header_name
    return str(ext) if ext else None
