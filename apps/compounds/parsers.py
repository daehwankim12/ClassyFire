"""Streaming file parsers for ClassyFire normalization service.

Public interface
----------------
parse_txt(file_obj, *, encoding="utf-8")
    -> Iterator[dict]  keys: raw, row_index

parse_csv_tsv(file_obj, *, smiles_col=None, id_col=None,
              delimiter=None, encoding="utf-8")
    -> Iterator[dict]  keys: raw, row_index, external_id (optional)

parse_sdf(file_obj)
    -> Iterator[dict]  keys: raw, row_index, external_id (optional)

Contract (language-agnostic):
- All parsers are GENERATORS — they never load the whole file into memory.
- row_index is 0-based and reflects the original order.
- Blank / whitespace-only lines in TXT/CSV are skipped.
- For CSV/TSV, smiles_col / id_col can be a column name (str) or index (int).
  When both are None the first column is used for raw; id_col=None means no id.
- delimiter=None auto-detects comma vs tab from the first 8 KB.
- For SDF, external_id is taken from an ``EXTERNAL_ID`` property if present.
- Parsers never raise on bad rows; callers should use normalize_row for that.
"""
from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from typing import IO

from rdkit import Chem


def parse_txt(
    file_obj: IO[bytes] | IO[str],
    *,
    encoding: str = "utf-8",
) -> Iterator[dict]:
    """Yield one dict per non-blank line: ``{raw, row_index}``."""
    text_stream = _ensure_text(file_obj, encoding)
    row_index = 0
    for line in text_stream:
        stripped = line.rstrip("\r\n")
        if stripped.strip():
            yield {"raw": stripped.strip(), "row_index": row_index}
            row_index += 1


def parse_csv_tsv(
    file_obj: IO[bytes] | IO[str],
    *,
    smiles_col: str | int | None = None,
    id_col: str | int | None = None,
    delimiter: str | None = None,
    encoding: str = "utf-8",
) -> Iterator[dict]:
    """Yield one dict per data row: ``{raw, row_index[, external_id]}``."""
    text_stream = _ensure_text(file_obj, encoding)

    if delimiter is None:
        # Read the first 8 KB to sniff; caller must prepend it back.
        peeked = _read_prefix(text_stream, 8192)
        delimiter = "\t" if "\t" in peeked else ","
        text_stream = _prepend(peeked, text_stream)

    reader = csv.DictReader(text_stream, delimiter=delimiter)
    fieldnames: list[str] = []  # populated after first row

    for row_index, row in enumerate(reader):
        if not fieldnames and reader.fieldnames:
            fieldnames = list(reader.fieldnames)

        raw_val = _resolve_col(row, fieldnames, smiles_col, default_index=0)
        if raw_val is None or not raw_val.strip():
            continue

        result: dict = {"raw": raw_val.strip(), "row_index": row_index}

        if id_col is not None:
            ext = _resolve_col(row, fieldnames, id_col, default_index=None)
            if ext is not None:
                result["external_id"] = ext.strip()

        yield result


def parse_sdf(
    file_obj: IO[bytes],
) -> Iterator[dict]:
    """Yield one dict per SDF record: ``{raw, row_index[, external_id]}``.

    True single-pass streaming: only one molecule block is held in memory at
    a time.  Lines are accumulated until the ``$$$$`` delimiter, then the block
    is parsed with RDKit and immediately yielded before the buffer is reset.
    A bad block yields an error-free dict (raw set, external_id absent) so
    callers using normalize_row will handle the error in isolation.
    """
    if hasattr(file_obj, "read"):
        text_stream: IO[str]
        if isinstance(file_obj, (io.RawIOBase, io.BufferedIOBase, io.BytesIO)):
            text_stream = io.TextIOWrapper(file_obj, encoding="utf-8", errors="replace")
        else:
            text_stream = file_obj  # type: ignore[assignment]
    else:
        text_stream = io.StringIO(file_obj.decode("utf-8", errors="replace"))  # type: ignore[union-attr]

    row_index = 0
    current_lines: list[str] = []

    for line in text_stream:
        if line.rstrip("\r\n") == "$$$$":
            # Only emit non-empty blocks.
            if current_lines:
                yield _parse_sdf_block("".join(current_lines), row_index)
                row_index += 1
                current_lines = []
        else:
            current_lines.append(line)

    # Handle a trailing block that has content but no closing $$$$ delimiter.
    if current_lines and any(ln.strip() for ln in current_lines):
        yield _parse_sdf_block("".join(current_lines), row_index)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_sdf_block(block: str, row_index: int) -> dict:
    """Parse a single SDF block string and return a parser result dict.

    Properties are extracted directly from the SDF data fields (``> <NAME>``
    / value lines) rather than via RDKit's prop dict, because
    ``Chem.MolFromMolBlock`` only parses up to ``M  END`` and does not
    populate SDF data fields.  The molecule is still parsed with RDKit to
    obtain the molecule name (``_Name``) as a fallback external id.
    """
    result: dict = {"raw": block.strip(), "row_index": row_index}

    # Extract SDF data properties from the text block directly.
    sdf_props = _extract_sdf_props(block)

    ext_id = sdf_props.get("EXTERNAL_ID") or sdf_props.get("ID")
    if not ext_id:
        # Fall back to molecule name from the molblock header line.
        try:
            mol = Chem.MolFromMolBlock(block, removeHs=False, sanitize=True)
            if mol is not None and mol.HasProp("_Name"):
                name = mol.GetProp("_Name").strip()
                if name:
                    ext_id = name
        except Exception:
            pass

    if ext_id:
        result["external_id"] = str(ext_id)
    return result


def _extract_sdf_props(block: str) -> dict[str, str]:
    """Extract SDF data fields (``> <NAME>`` / value) from a block string.

    Scanning starts after the ``M  END`` line.  Only the first non-blank value
    line for each property is captured (standard SDF convention).
    """
    props: dict[str, str] = {}
    lines = block.splitlines()
    in_data = False
    current_prop: str | None = None
    for line in lines:
        stripped = line.strip()
        if not in_data:
            if stripped == "M  END":
                in_data = True
            continue
        # Inside data section.
        if stripped.startswith(">") and "<" in stripped:
            # e.g.  "> <EXTERNAL_ID>" or "> <EXTERNAL_ID>  (optional comment)"
            start = stripped.index("<") + 1
            end = stripped.index(">", start)
            current_prop = stripped[start:end]
        elif current_prop is not None:
            if stripped:
                props[current_prop] = stripped
                current_prop = None  # Only capture first value line.
    return props


def _ensure_text(file_obj: IO[bytes] | IO[str], encoding: str) -> IO[str]:
    """Wrap a binary stream in a TextIOWrapper if needed."""
    if isinstance(file_obj, (io.RawIOBase, io.BufferedIOBase)):
        return io.TextIOWrapper(file_obj, encoding=encoding)
    if isinstance(file_obj, io.BytesIO):
        return io.TextIOWrapper(file_obj, encoding=encoding)
    return file_obj  # type: ignore[return-value]


def _to_binary(file_obj: IO[str]) -> io.BytesIO:
    return io.BytesIO(file_obj.read().encode("utf-8"))


def _read_prefix(stream: IO[str], n: int) -> str:
    """Consume up to *n* chars from *stream* and return them.

    The caller is responsible for prepending the returned string back to the
    stream (via :func:`_prepend`) before further reads.  The stream is
    permanently advanced by the number of characters returned.
    """
    return stream.read(n)


def _prepend(prefix: str, stream: IO[str]) -> IO[str]:
    """Return a new stream that yields *prefix* then the remaining *stream*."""
    remaining = stream.read()
    return io.StringIO(prefix + remaining)


def _resolve_col(
    row: dict,
    fieldnames: list[str],
    col: str | int | None,
    default_index: int | None,
) -> str | None:
    if col is None:
        if default_index is None:
            return None
        if default_index < len(fieldnames):
            return row.get(fieldnames[default_index])
        return None
    if isinstance(col, int):
        if col < len(fieldnames):
            return row.get(fieldnames[col])
        return None
    return row.get(col)
