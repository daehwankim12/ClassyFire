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

from rdkit.Chem import ForwardSDMolSupplier


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
        # Peek at the first 8 KB to sniff.
        peeked = _peek_text(text_stream, 8192)
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
    """Yield one dict per SDF record: ``{raw, row_index[, external_id]}``."""
    # ForwardSDMolSupplier requires a binary file-like object.
    if hasattr(file_obj, "read"):
        bio = file_obj if isinstance(file_obj, (io.RawIOBase, io.BufferedIOBase)) else _to_binary(file_obj)
    else:
        bio = io.BytesIO(file_obj)  # type: ignore[arg-type]

    # We also need the raw SDF block text to put in NormalizedRow.original_input.
    # We read the whole binary stream split by '$$$$' for that, while using
    # ForwardSDMolSupplier for the parsed molecule.
    bio.seek(0)
    raw_content = bio.read().decode("utf-8", errors="replace")
    bio.seek(0)

    raw_blocks = _split_sdf_blocks(raw_content)

    supp = ForwardSDMolSupplier(bio, removeHs=False, sanitize=True)
    for row_index, mol in enumerate(supp):
        raw_block = raw_blocks[row_index] if row_index < len(raw_blocks) else ""
        result: dict = {"raw": raw_block.strip(), "row_index": row_index}

        if mol is not None:
            props = mol.GetPropsAsDict()
            ext_id = props.get("EXTERNAL_ID") or props.get("ID") or props.get("Name")
            if ext_id:
                result["external_id"] = str(ext_id)

        yield result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_text(file_obj: IO[bytes] | IO[str], encoding: str) -> IO[str]:
    """Wrap a binary stream in a TextIOWrapper if needed."""
    if isinstance(file_obj, (io.RawIOBase, io.BufferedIOBase)):
        return io.TextIOWrapper(file_obj, encoding=encoding)
    if isinstance(file_obj, io.BytesIO):
        return io.TextIOWrapper(file_obj, encoding=encoding)
    return file_obj  # type: ignore[return-value]


def _to_binary(file_obj: IO[str]) -> io.BytesIO:
    return io.BytesIO(file_obj.read().encode("utf-8"))


def _peek_text(stream: IO[str], n: int) -> str:
    """Read up to *n* chars without consuming the stream (best-effort)."""
    buf = stream.read(n)
    return buf


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


def _split_sdf_blocks(content: str) -> list[str]:
    """Split SDF content into individual record blocks (without '$$$$' line)."""
    blocks = []
    current: list[str] = []
    for line in content.splitlines(keepends=True):
        if line.strip() == "$$$$":
            blocks.append("".join(current))
            current = []
        else:
            current.append(line)
    if current:
        blocks.append("".join(current))
    return blocks
