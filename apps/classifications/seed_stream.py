"""Streaming seed importer.

Reads a zstd-compressed TSV file and streams rows into PostgreSQL using
psycopg ``COPY``, without ever decompressing the whole file to disk.

Checkpoint / resume
-------------------
The ``SeedImport`` row's ``audit_result`` JSONB field stores a ``checkpoint``
key with the last-committed row offset.  Pass ``resume=True`` to skip
already-loaded rows and continue from that offset.

View approach
-------------
For LITE/fixture imports, rows go directly into the base ``classyfire_seed``
table.  The ``classyfire_seed_current`` view (created/replaced by
``seed_activate``) points at the versioned physical table for the active import.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import psycopg.sql as pgsql

logger = logging.getLogger(__name__)

# Column order in the TSV (must match the fixture).
TSV_COLUMNS = [
    "inchikey",
    "inchikey14",
    "pubchem_cid",
    "zinc_id",
    "representative_smiles",
    "kingdom_id",
    "superclass_id",
    "class_id",
    "subclass_id",
    "direct_parent_id",
    "other_labels",
]
TSV_COLUMN_SET = set(TSV_COLUMNS)
ZENODO_V3_COLUMNS = {
    "inchikey",
    "cid",
    "zinc_id",
    "smiles",
    "chemont_tree_json",
    "chemont_other_json",
}

# DB columns that map to the physical ``classyfire_seed`` table.
# Note: model field ``class_node`` uses db_column ``class_id``.
DB_COLUMNS = [
    "inchikey",
    "inchikey14",
    "pubchem_cid",
    "zinc_id",
    "representative_smiles",
    "kingdom_id",
    "superclass_id",
    "class_id",
    "subclass_id",
    "direct_parent_id",
    "other_labels",
    "seed_source_id",
    "seed_version",
]

INCHIKEY_RE = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")


class StreamImportError(Exception):
    """Raised on fatal stream import errors."""


def _null_or_int(val: str) -> str:
    """Return '\\N' (psycopg COPY NULL) for empty/whitespace strings."""
    v = val.strip()
    return r"\N" if not v else v


def _null_or_text(val: str) -> str:
    v = val.strip()
    return r"\N" if not v else v


def _copy_field(val: str) -> str:
    """Escape a single value for PostgreSQL text COPY."""
    if val == r"\N":
        return val
    return (
        val.replace("\\", "\\\\")
        .replace("\t", r"\t")
        .replace("\n", r"\n")
        .replace("\r", r"\r")
    )


def _format_copy_line(values: list[str]) -> str:
    return "\t".join(_copy_field(value) for value in values) + "\n"


def _detect_schema(
    fieldnames: Sequence[str] | None,
) -> Literal["normalized", "zenodo_v3"]:
    if fieldnames is None:
        raise StreamImportError("Seed TSV has no header row.")

    fields = set(fieldnames)
    if TSV_COLUMN_SET <= fields:
        return "normalized"
    if ZENODO_V3_COLUMNS <= fields:
        return "zenodo_v3"

    raise StreamImportError(
        "Seed TSV missing supported columns: "
        f"normalized missing={TSV_COLUMN_SET - fields}; "
        f"zenodo_v3 missing={ZENODO_V3_COLUMNS - fields}"
    )


def _parse_tree_slots(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StreamImportError(f"Invalid chemont_tree_json: {raw!r}") from exc

    if not isinstance(value, list) or len(value) != 5:
        raise StreamImportError("chemont_tree_json must be a five-element array")

    slots: list[str] = []
    for item in value:
        if item is None:
            slots.append(r"\N")
        elif isinstance(item, int):
            slots.append(str(item))
        else:
            raise StreamImportError(
                "chemont_tree_json values must be integer IDs or null"
            )
    return slots


def _normalize_json_text(raw: str) -> str:
    value = raw.strip()
    if not value:
        return r"\N"
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise StreamImportError(f"Invalid chemont_other_json: {raw!r}") from exc
    return json.dumps(parsed, separators=(",", ":"))


def _db_values_for_row(
    row: dict[str, str],
    schema: Literal["normalized", "zenodo_v3"],
    seed_import_id: int,
    seed_version: str,
) -> list[str]:
    inchikey = row.get("inchikey", "").strip()

    if schema == "normalized":
        values = [
            inchikey,
            row.get("inchikey14", "").strip(),
            _null_or_int(row.get("pubchem_cid", "")),
            _null_or_text(row.get("zinc_id", "")),
            _null_or_text(row.get("representative_smiles", "")),
            _null_or_int(row.get("kingdom_id", "")),
            _null_or_int(row.get("superclass_id", "")),
            _null_or_int(row.get("class_id", "")),
            _null_or_int(row.get("subclass_id", "")),
            _null_or_int(row.get("direct_parent_id", "")),
            _null_or_text(row.get("other_labels", "")),
        ]
    else:
        kingdom_id, superclass_id, class_id, subclass_id, direct_parent_id = (
            _parse_tree_slots(row.get("chemont_tree_json", ""))
        )
        values = [
            inchikey,
            inchikey[:14],
            _null_or_int(row.get("cid", "")),
            _null_or_text(row.get("zinc_id", "")),
            _null_or_text(row.get("smiles", "")),
            kingdom_id,
            superclass_id,
            class_id,
            subclass_id,
            direct_parent_id,
            _normalize_json_text(row.get("chemont_other_json", "")),
        ]

    return [*values, str(seed_import_id), seed_version]


def stream_import(
    source_path: str | Path,
    seed_import_id: int,
    seed_version: str,
    *,
    target_table: str = "classyfire_seed",
    resume: bool = False,
    shard_size: int = 5000,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Stream-import a zstd-compressed TSV into *target_table*.

    Parameters
    ----------
    source_path:
        Local path to the ``.tsv.zst`` file.
    seed_import_id:
        PK of the ``SeedImport`` row tracking this import.
    seed_version:
        Version string to write into every row.
    target_table:
        Physical table to write into (default ``classyfire_seed``).
    resume:
        If True, skip rows already imported (based on checkpoint in SeedImport).
    shard_size:
        Number of rows per COPY shard (checkpoint granularity).
    dry_run:
        If True, parse and validate but do not write to DB.

    Returns
    -------
    dict with keys ``rows_written``, ``rows_skipped``, ``duplicates``,
    ``checkpoint_offset``.
    """
    import zstandard as zstd

    from apps.operations.models import SeedImport

    source = Path(source_path)
    if not source.exists():
        raise FileNotFoundError(f"Seed source not found: {source}")

    seed_import = SeedImport.objects.get(pk=seed_import_id)

    # Determine resume offset.
    resume_offset = 0
    if resume:
        checkpoint = (seed_import.audit_result or {}).get("checkpoint", {})
        resume_offset = checkpoint.get("offset", 0)
        if resume_offset:
            logger.info("Resuming from offset %d", resume_offset)

    rows_written = 0
    rows_skipped = 0
    duplicates = 0
    offset = 0

    dctx = zstd.ZstdDecompressor()

    with open(source, "rb") as raw_fh:
        with dctx.stream_reader(raw_fh) as reader:
            text_reader = io.TextIOWrapper(reader, encoding="utf-8")
            csv_reader = csv.DictReader(text_reader, delimiter="\t")
            schema = _detect_schema(csv_reader.fieldnames)

            if dry_run:
                # Parse and count rows without writing.
                for row in csv_reader:
                    offset += 1
                    _db_values_for_row(row, schema, seed_import_id, seed_version)
                seed_import.actual_row_count = offset
                seed_import.import_status = "DRY_RUN"
                seed_import.save(update_fields=["actual_row_count", "import_status"])
                return {
                    "rows_written": 0,
                    "rows_skipped": offset,
                    "duplicates": 0,
                    "checkpoint_offset": 0,
                    "dry_run": True,
                }

            # Get DB connection for COPY.
            from django.db import connection

            with connection.cursor() as cursor:
                shard_buf: list[list[str]] = []

                def flush_shard(buf: list[list[str]]) -> tuple[int, int]:
                    """COPY a shard to the target table. Returns (written, dupes)."""
                    if not buf:
                        return 0, 0
                    written = 0
                    dupes = 0
                    copy_data = "".join(_format_copy_line(values) for values in buf)
                    tbl = pgsql.Identifier(target_table)
                    col_list = pgsql.SQL(", ").join(
                        pgsql.Identifier(c) for c in DB_COLUMNS
                    )
                    copy_sql = pgsql.SQL(
                        "COPY {} ({}) FROM STDIN WITH (FORMAT text, NULL '\\N')"
                    ).format(tbl, col_list).as_string(cursor.connection)
                    try:
                        with cursor.connection.cursor() as cur:
                            with cur.copy(copy_sql) as copy:
                                copy.write(copy_data.encode("utf-8"))
                        written = len(buf)
                    except Exception as exc:
                        # On duplicate key, fall back to row-by-row insert.
                        logger.warning("Bulk COPY failed (%s), falling back.", exc)
                        cursor.connection.rollback()
                        insert_sql = pgsql.SQL(
                            "INSERT INTO {} ({}) VALUES ({}) "
                            "ON CONFLICT (inchikey) DO NOTHING"
                        ).format(
                            tbl,
                            col_list,
                            pgsql.SQL(", ").join(
                                pgsql.Placeholder() for _ in DB_COLUMNS
                            ),
                        ).as_string(cursor.connection)
                        for raw_parts in buf:
                            # COPY uses '\N' for NULL; convert to None for
                            # parameterized INSERT bindings. Use the raw value
                            # list, not escaped COPY text, to preserve strings.
                            parts = [
                                None if v == r"\N" else v for v in raw_parts
                            ]
                            inchikey = raw_parts[0] if raw_parts else ""
                            try:
                                cursor.execute(insert_sql, parts)
                                if cursor.rowcount == 0:
                                    dupes += 1
                                    logger.debug("Duplicate inchikey: %s", inchikey)
                                else:
                                    written += 1
                            except Exception as row_exc:
                                logger.error("Row insert failed: %s", row_exc)
                    return written, dupes

                for row in csv_reader:
                    offset += 1
                    if offset <= resume_offset:
                        rows_skipped += 1
                        continue

                    shard_buf.append(
                        _db_values_for_row(row, schema, seed_import_id, seed_version)
                    )

                    if len(shard_buf) >= shard_size:
                        w, d = flush_shard(shard_buf)
                        rows_written += w
                        duplicates += d
                        shard_buf = []
                        # At-least-once checkpoint: a crash between flush and
                        # here causes the shard to be re-attempted on resume,
                        # which is safe because the fallback INSERT uses
                        # ON CONFLICT DO NOTHING (idempotent).
                        _save_checkpoint(seed_import, offset)

                # Flush remaining rows.
                if shard_buf:
                    w, d = flush_shard(shard_buf)
                    rows_written += w
                    duplicates += d
                    # At-least-once checkpoint: see comment above.
                    _save_checkpoint(seed_import, offset)

    # Update SeedImport row.
    seed_import.actual_row_count = (seed_import.actual_row_count or 0) + rows_written
    seed_import.duplicate_count = (seed_import.duplicate_count or 0) + duplicates
    seed_import.import_status = "IMPORTED"
    seed_import.save(update_fields=["actual_row_count", "duplicate_count", "import_status"])

    logger.info(
        "Stream import complete: written=%d skipped=%d dupes=%d",
        rows_written,
        rows_skipped,
        duplicates,
    )
    return {
        "rows_written": rows_written,
        "rows_skipped": rows_skipped,
        "duplicates": duplicates,
        "checkpoint_offset": offset,
        "dry_run": False,
    }


def _save_checkpoint(seed_import: Any, offset: int) -> None:
    """Persist checkpoint offset to ``SeedImport.audit_result``."""
    result = seed_import.audit_result or {}
    result["checkpoint"] = {"offset": offset}
    seed_import.audit_result = result
    seed_import.save(update_fields=["audit_result"])
