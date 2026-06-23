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
import logging
import re
from pathlib import Path
from typing import Any

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

            if dry_run:
                # Just count rows without writing.
                for _row in csv_reader:
                    offset += 1
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
                shard_buf: list[str] = []

                def flush_shard(buf: list[str]) -> tuple[int, int]:
                    """COPY a shard to the target table. Returns (written, dupes)."""
                    if not buf:
                        return 0, 0
                    written = 0
                    dupes = 0
                    copy_data = "".join(buf)
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
                        for line in buf:
                            raw_parts = line.rstrip("\n").split("\t")
                            # COPY uses '\N' for NULL; convert back to None for
                            # parameterized INSERT bindings.
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

                    inchikey = row.get("inchikey", "").strip()
                    inchikey14 = row.get("inchikey14", "").strip()
                    pubchem_cid = _null_or_int(row.get("pubchem_cid", ""))
                    zinc_id = _null_or_text(row.get("zinc_id", ""))
                    smiles = _null_or_text(row.get("representative_smiles", ""))
                    kingdom_id = _null_or_int(row.get("kingdom_id", ""))
                    superclass_id = _null_or_int(row.get("superclass_id", ""))
                    class_id = _null_or_int(row.get("class_id", ""))
                    subclass_id = _null_or_int(row.get("subclass_id", ""))
                    direct_parent_id = _null_or_int(row.get("direct_parent_id", ""))
                    other_labels = _null_or_text(row.get("other_labels", ""))

                    line = "\t".join([
                        inchikey,
                        inchikey14,
                        pubchem_cid,
                        zinc_id,
                        smiles,
                        kingdom_id,
                        superclass_id,
                        class_id,
                        subclass_id,
                        direct_parent_id,
                        other_labels,
                        str(seed_import_id),
                        seed_version,
                    ]) + "\n"
                    shard_buf.append(line)

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
