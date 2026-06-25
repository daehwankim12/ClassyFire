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
from collections.abc import Iterator
from typing import IO

from django.conf import settings

from apps.core.enums import DetectedInputType
from apps.core.security import (
    MAX_SDF_PROPERTY_LEN,
    MAX_STRUCTURE_LEN,
)


def _max_structure_len() -> int:
    """Bounded cap, read from Django settings so it can be overridden in tests.

    Falls back to the module constant when settings are unavailable (the two are
    kept in lockstep: ``settings.MAX_STRUCTURE_LEN`` defaults to
    ``apps.core.security.MAX_STRUCTURE_LEN``).
    """
    return int(getattr(settings, "MAX_STRUCTURE_LEN", MAX_STRUCTURE_LEN))

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
    yield from iter_text_records(file, filename=filename, content_type=content_type)


# ---------------------------------------------------------------------------
# Bounded byte-chunk scanner (M-02)
# ---------------------------------------------------------------------------


def iter_bounded_lines(
    binary_file: IO[bytes],
    *,
    max_bytes: int,
    chunk_size: int = 64 * 1024,
) -> Iterator[tuple[bytes | None, int]]:
    """Yield physical lines from *binary_file* without materialising any single
    line larger than ``max_bytes``.

    Reads the upload in ``chunk_size`` byte blocks (never a single unbounded
    ``read()``) and splits on ``\\n``. A line whose accumulated length crosses
    ``max_bytes`` is NOT buffered whole: accumulation stops, the rest of the line
    is counted (not stored), and the line is yielded as ``(None, overflow_len)``
    where ``overflow_len`` is the TRUE total byte length of the physical line.
    Normal lines are yielded as ``(line_bytes, len(line_bytes))`` (the trailing
    ``\\n`` is stripped; a trailing ``\\r`` is left for the decoder to handle).
    """
    buffer = bytearray()
    overflow = False
    overflow_len = 0
    while True:
        chunk = binary_file.read(chunk_size)
        if not chunk:
            if buffer or overflow:
                if overflow:
                    yield None, overflow_len
                else:
                    yield bytes(buffer), len(buffer)
            return
        for byte in chunk:
            if byte == 10:  # newline terminates the physical line
                if overflow:
                    yield None, overflow_len
                    overflow = False
                    overflow_len = 0
                    buffer.clear()
                else:
                    yield bytes(buffer), len(buffer)
                    buffer.clear()
                continue
            if overflow:
                overflow_len += 1
                continue
            if len(buffer) >= max_bytes:
                overflow = True
                overflow_len = len(buffer) + 1
                buffer.clear()
                continue
            buffer.append(byte)


def iter_text_records(
    binary_file: IO[bytes],
    *,
    filename: str | None = None,
    content_type: str | None = None,
) -> Iterator[dict]:
    """Public bounded-scanner entry point: dispatch by format, drive the
    byte-chunk scanner, and yield parser-style row dicts.

    Every format (TEXT/CSV/TSV/SDF) reads through :func:`iter_bounded_lines`, so
    a single line/record with no newline can never be slurped whole before the
    ``MAX_STRUCTURE_LEN`` cap is applied.
    """
    fmt = detect_upload_format(filename, content_type)
    if fmt is UploadFormat.SDF:
        yield from _iter_sdf(binary_file)
    elif fmt in (UploadFormat.CSV, UploadFormat.TSV):
        delimiter = "," if fmt is UploadFormat.CSV else "\t"
        yield from _iter_csv_tsv(binary_file, delimiter)
    else:
        yield from _iter_text(binary_file)


# ---------------------------------------------------------------------------
# Streaming text / CSV / TSV
# ---------------------------------------------------------------------------


def _decode_line(raw: bytes, *, first: bool) -> str:
    """Decode one bounded physical line. The first line uses ``utf-8-sig`` to
    strip a leading BOM; subsequent lines use plain ``utf-8`` (errors replaced)."""
    encoding = "utf-8-sig" if first else "utf-8"
    return raw.decode(encoding, errors="replace").rstrip("\r")


def _overflow_row(overflow_length: int, row_index: int) -> dict:
    """Parser-style row for a physical line that exceeded the byte cap.

    The raw value is a fixed sentinel — the real bytes were never buffered — and
    the TRUE total length is carried in ``overflow_length`` for the operator
    message. Such a row must NOT be sent to RDKit (services rejects on
    ``parse_error``)."""
    return {
        "raw": "[truncated]",
        "row_index": row_index,
        "parse_error": "ROW_TOO_LONG",
        "overflow_length": overflow_length,
        "input_type": DetectedInputType.UNKNOWN,
    }


def _iter_text(file: IO[bytes]) -> Iterator[dict]:
    """Plain-text fallback: one non-blank line → one row, bounded per line."""
    row_index = 0
    first = True
    for line_bytes, length in iter_bounded_lines(file, max_bytes=_max_structure_len()):
        if line_bytes is None:
            yield _overflow_row(length, row_index)
            row_index += 1
            first = False
            continue
        stripped = _decode_line(line_bytes, first=first).strip()
        first = False
        if not stripped:
            continue
        yield {"raw": stripped, "row_index": row_index}
        row_index += 1


def _iter_csv_tsv(file: IO[bytes], delimiter: str) -> Iterator[dict]:
    """Stream a CSV/TSV upload: a ``smiles``-like column is ``raw``; an
    ``id``-like column (if present) is ``external_id``. Header row is consumed
    once. Each physical line is read bounded and parsed independently, so memory
    stays bounded to a single line."""
    header: list[str] | None = None
    id_index: int | None = None
    smiles_index = 0
    row_index = 0
    first = True

    for line_bytes, length in iter_bounded_lines(file, max_bytes=_max_structure_len()):
        if line_bytes is None:
            # An over-cap header line means we cannot dispatch columns; surface it
            # as an error row and keep scanning.
            yield _overflow_row(length, row_index)
            row_index += 1
            first = False
            continue
        text = _decode_line(line_bytes, first=first)
        first = False
        fields = next(csv.reader([text], delimiter=delimiter), [])

        if header is None:
            header = fields
            id_index = _find_id_column(header)
            smiles_index = _find_smiles_column(header)
            continue

        if not fields:
            continue
        raw = fields[smiles_index].strip() if smiles_index < len(fields) else ""
        if not raw:
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
    row_index = 0
    buffer: list[str] = []
    buffer_bytes = 0
    overflowed = False

    # Each bounded line is read in ≤64KB chunks; a single line longer than the
    # cap arrives as ``None`` and overflows the current record instead of being
    # materialised. We re-append ``\n`` to reconstruct the record block so the
    # V2000 title / leading-blank line positions are preserved exactly.
    for line_bytes, _length in iter_bounded_lines(file, max_bytes=MAX_SDF_RECORD_LEN):
        if line_bytes is None:
            overflowed = True
            continue
        line = _decode_line(line_bytes, first=False)
        if line == "$$$$":
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

        line += "\n"
        line_len = len(line)
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
