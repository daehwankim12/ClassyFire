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

import hashlib
import os
import time
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from apps.batches.models import BatchItem, BatchJob
from apps.core.enums import BatchItemStatus

# Number of items to INSERT per bulk_create call (streaming, not all-in-RAM).
CHUNK_SIZE = 500


# ---------------------------------------------------------------------------
# Atomic upload spooling (H-04)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpooledUpload:
    """Metadata for an upload that was atomically spooled to a durable artifact."""

    relative_path: str
    original_name: str
    content_type: str
    size_bytes: int
    sha256: str


class UploadTooLarge(ValueError):  # noqa: N818 - public name fixed by test/spec contract
    """Upload exceeded configured byte limit during streaming."""


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_upload_root(value: str) -> Path:
    """Resolve and sanity-check the configured upload artifact root."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("UPLOAD_ARTIFACT_DIR is empty")
    root = Path(raw).resolve()
    forbidden = {Path("/").resolve(), Path.cwd().resolve()}
    if root in forbidden:
        raise ValueError(f"unsafe upload artifact root: {root}")
    return root


def spool_upload_file(upload: object, *, max_bytes: int | None = None) -> SpooledUpload:
    """Atomically spool *upload* to ``<UPLOAD_ARTIFACT_DIR>/<uuid><suffix>``.

    Streams ``upload.chunks()`` to a ``*.tmp`` file, enforcing ``max_bytes`` mid
    stream (raising :class:`UploadTooLarge` and leaving NO files), hashing as it
    goes. On success the tmp is fsynced and atomically ``replace()``-d onto the
    final path, then the parent dir is fsynced — so a reader never sees a partial
    final file and a crash never leaves a ``*.tmp`` masquerading as complete. On
    ANY exception both tmp and final are unlinked.
    """
    root = validate_upload_root(getattr(settings, "UPLOAD_ARTIFACT_DIR", ""))
    root.mkdir(parents=True, exist_ok=True)
    max_bytes = max_bytes or settings.MAX_UPLOAD_MB * 1024 * 1024
    name = getattr(upload, "name", "upload.bin") or "upload.bin"
    suffix = Path(name).suffix.lower()
    stem = uuid.uuid4().hex
    tmp_path = root / f"{stem}.tmp"
    final_path = root / f"{stem}{suffix}"
    digest = hashlib.sha256()
    size = 0
    try:
        with tmp_path.open("wb") as out:
            for chunk in upload.chunks():  # type: ignore[attr-defined]
                size += len(chunk)
                if size > max_bytes:
                    raise UploadTooLarge(f"upload exceeds {max_bytes} bytes")
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        tmp_path.replace(final_path)
        _fsync_directory(root)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        raise
    return SpooledUpload(
        relative_path=final_path.name,
        original_name=name,
        content_type=getattr(upload, "content_type", "") or "",
        size_bytes=size,
        sha256=digest.hexdigest(),
    )


def spool_text_input(text: str, *, max_bytes: int | None = None) -> SpooledUpload:
    """Spool a pasted text blob as an artifact, reusing the atomic file path.

    Wraps the encoded text in a minimal chunked-upload shim so text-paste and
    file uploads share one durable, crash-safe spool path.
    """

    class _TextUpload:
        name = "text_input.txt"
        content_type = "text/plain"

        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def chunks(self) -> Iterator[bytes]:
            yield self._payload

    return spool_upload_file(_TextUpload(text.encode("utf-8")), max_bytes=max_bytes)


def iter_uploaded_records_from_path(
    artifact: Path,
    *,
    original_name: str,
    content_type: str,
) -> Iterator[dict]:
    """Open a spooled artifact and yield parser-style rows via the bounded parser.

    Reuses :func:`apps.batches.upload.iter_upload_rows` so the worker parses the
    durable artifact exactly the way the old in-request path parsed the live
    upload (suffix+content-type dispatch, bounded streaming).
    """
    from apps.batches.upload import iter_upload_rows

    with artifact.open("rb") as handle:
        yield from iter_upload_rows(
            handle,
            filename=original_name,
            content_type=content_type,
        )


def cleanup_stale_upload_temps(*, max_age_seconds: int) -> int:
    """Remove stale ``*.tmp`` partials left by crashed spool writes."""
    root = validate_upload_root(getattr(settings, "UPLOAD_ARTIFACT_DIR", ""))
    if not root.exists():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for path in root.glob("*.tmp"):
        if path.stat().st_mtime <= cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


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
            submitted_type=row.get("input_type") or None,
            parse_error=row.get("parse_error"),
            overflow_length=row.get("overflow_length"),
            status=BatchItemStatus.PENDING_NORMALIZATION,
        )
        chunk.append(item)

        if len(chunk) >= CHUNK_SIZE:
            BatchItem.objects.bulk_create(chunk)
            chunk = []

    if chunk:
        BatchItem.objects.bulk_create(chunk)

    return count
