"""Streaming ingest of input rows into BatchItem records.

Public interface
----------------
ingest_rows(
    job: BatchJob,
    rows: Iterable[dict],
) -> None
    Bulk-creates BatchItem rows from an iterable of parser dicts
    (keys: raw, row_index, external_id?), preserving order and duplicates.
    Does NOT run local processing — that is services.process_job_items.

parse_text_input(text: str) -> Iterator[dict]
    Splits a plain-text multi-line input into parser-style row dicts.

Contract:
- Row order is preserved exactly (row_order = position in the iterable).
- Duplicates are preserved (same input twice → two items).
- external_id is carried through from the parser dict when present.
- Rows are bulk-created in chunks of CHUNK_SIZE for memory efficiency.
- Never raises on a bad row — items enter with PENDING_NORMALIZATION status
  and services.py transitions them after normalization.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator

from apps.batches.models import BatchItem, BatchJob
from apps.core.enums import BatchItemStatus

# Number of items to INSERT per bulk_create call (streaming, not all-in-RAM).
CHUNK_SIZE = 500


def parse_text_input(text: str) -> Iterator[dict]:
    """Split a plain-text multi-line blob into parser-style row dicts.

    Blank / whitespace-only lines are skipped.  row_index is 0-based.
    """
    row_index = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            yield {"raw": stripped, "row_index": row_index}
            row_index += 1


def ingest_rows(
    job: BatchJob,
    rows: Iterable[dict],
    *,
    max_rows: int | None = None,
) -> int:
    """Bulk-insert BatchItem rows for *job* from the parser output *rows*.

    Items are created with status PENDING_NORMALIZATION; services.py will
    update them after running local processing.

    Parameters
    ----------
    job:
        The parent BatchJob.
    rows:
        Iterable of dicts with at least ``raw`` and ``row_index`` keys;
        optionally ``external_id``.
    max_rows:
        Optional hard cap. When the streamed input would exceed it,
        :class:`apps.batches.services.RowCapExceededError` is raised mid-stream
        (before buffering the whole input) so the caller can reject the upload.

    Returns the number of rows ingested.
    """
    # Local import avoids a circular import at module load.
    from apps.batches.services import RowCapExceededError

    chunk: list[BatchItem] = []
    count = 0

    for row in rows:
        count += 1
        if max_rows is not None and count > max_rows:
            raise RowCapExceededError(max_rows)
        item = BatchItem(
            job=job,
            row_order=row["row_index"],
            original_input=row["raw"],
            external_id=row.get("external_id"),
            status=BatchItemStatus.PENDING_NORMALIZATION,
        )
        chunk.append(item)

        if len(chunk) >= CHUNK_SIZE:
            BatchItem.objects.bulk_create(chunk)
            chunk = []

    if chunk:
        BatchItem.objects.bulk_create(chunk)

    return count
