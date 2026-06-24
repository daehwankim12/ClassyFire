"""Export functionality for batch job results.

Public interface
----------------
create_export(job, fmt, secret) -> (token, expires_at)
    Create (or reuse) an export file for the given job and format.
    Validates secret. Returns a URL-safe token and expiry datetime.

create_export_for_authorized_job(job, fmt) -> (token, expires_at)
    Create (or reuse) an export after the caller has already authorized job access.

get_export_metadata(token) -> dict | None
    Return export metadata for auth decisions, or None.

get_export_path(token) -> Path | None
    Return the file path for a valid, non-expired export token, or None.

EXPORT_DIR: Path — directory where export files are stored.

Formats supported: csv, tsv, json, csv.gz, tsv.gz

Citation header (FR-028): prepended to CSV/TSV formats as # comment lines.
Injection escaping (FR-028): cells starting with =, +, -, @ are prefixed with '.
Token expiry: settings.EXPORT_RETENTION_DAYS days from creation.
Deduplication: same job+format with valid non-expired token returns same token.

Implementation notes (Gate-2 WS-E / F-12)
------------------------------------------
- Export generation is STREAMING and MEMORY-BOUNDED: rows are iterated with
  .iterator(chunk_size=2000) and written one at a time to a temp file, then
  atomically renamed into place with os.replace() so a partial file is never
  served.
- Gzip formats use gzip.GzipFile wrapping the temp file — NOT gzip.compress on
  a full in-memory byte string.
- JSON export writes '[', then comma-separated objects, then ']' without
  building a list in memory.
- A SINGLE streaming pass (_write_export_to_temp) writes the file AND computes
  the rows_signature simultaneously.  Because the signature is only known after
  the scan, the flow is write-to-temp -> dedup-check -> finalize: if a valid
  artifact already has that signature, the temp file is discarded and the token
  reused; otherwise the temp is atomically renamed (os.replace) into place. This
  means at most ONE full BatchItem scan per create (no double scan).
- ExportArtifact (DB model) is the source of truth for metadata/dedup/expiry.
  A JSON sidecar (.meta.json) is also written for backward compatibility with
  existing tests that call _meta_path/_load_meta directly.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import logging
import secrets
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.utils import timezone

from apps.batches.models import BatchItem, BatchJob, ExportArtifact
from apps.batches.storage import ArtifactStorage, get_artifact_storage

if TYPE_CHECKING:
    from apps.classifications.models import ClassificationResult

logger = logging.getLogger(__name__)

EXPORT_DIR: Path = Path(getattr(settings, "EXPORT_DIR", settings.BASE_DIR / "export_cache"))

CITATION_LINES = [
    "# ClassyFire Batch Cache Web — Export",
    "# Citation: Djoumbou Feunang et al., J. Cheminformatics 8:61 (2016). DOI: 10.1186/s13321-016-0174-y",
    "# Seed dataset: Zenodo record 20472700 — InChIKey-Deduplicated ClassyFire/ChemOnt Label Collection v3 (CC BY 4.0)",
    "# ChemOnt taxonomy: http://classyfire.wishartlab.com/tax_nodes/C0000000",
    "# See NOTICE.md for full attribution.",
]

CITATION_TEXT = "\n".join(CITATION_LINES) + "\n"

CITATION_DICT = (
    "ClassyFire Batch Cache Web — Export. "
    "Citation: Djoumbou Feunang et al., J. Cheminformatics 8:61 (2016). DOI: 10.1186/s13321-016-0174-y. "
    "Seed dataset: Zenodo record 20472700 (CC BY 4.0). "
    "ChemOnt: http://classyfire.wishartlab.com/tax_nodes/C0000000"
)

COLUMNS = [
    "InChIKey",
    "ClassyFy Status",
    "Kingdom",
    "Superclass",
    "Class",
    "Subclass",
    "Parent Level 1",
    "Parent Level 2",
    "Parent Level 3",
    # Audit columns (Gate-2 WS-E / F-12)
    "Canonical SMILES",
    "InChI",
    "Requested InChIKey",
    "Returned InChIKey",
    "Remote Query ID",
    "Classification Version",
]

# Statuses that warrant a classification lookup
_FOUND_STATUSES = frozenset([
    "FOUND_LIVE_CACHE",
    "FOUND_SEED_CACHE",
    "FOUND_WISHART_ENTITY",
    "COMPLETE_AFTER_WISHART",
])


def _artifact_storage() -> ArtifactStorage:
    """Return export artifact storage rooted at the compatibility EXPORT_DIR."""
    return get_artifact_storage(root=EXPORT_DIR)


def _escape_cell(value: str | int | None) -> str:
    """FR-028: Escape CSV/TSV cell values to prevent formula injection.

    Detects dangerous leading character AFTER stripping leading whitespace,
    so \" =cmd\" and \"\\t+cmd\" are also escaped (leading-whitespace bypass fix).
    """
    if value is None:
        return ""
    s = str(value)
    # Check the first non-whitespace character for dangerous formula starters.
    # We still escape even if the raw value begins with whitespace — attackers
    # can use ` =cmd` to bypass a check on s[0] alone.
    stripped = s.lstrip()
    if stripped and stripped[0] in ("=", "+", "-", "@"):
        return "'" + s
    if "\t" in s or "\r" in s:
        return "'" + s
    return s


def _snapshot_label(snapshot: dict[str, object], key: str) -> dict[str, object] | None:
    value = snapshot.get(key)
    return value if isinstance(value, dict) else None


def _label_name(snapshot: dict[str, object], key: str) -> str:
    label = _snapshot_label(snapshot, key)
    if label is None:
        return ""
    name = label.get("name")
    return str(name) if name is not None else ""


def _label_chemont_id(snapshot: dict[str, object], key: str) -> str | None:
    label = _snapshot_label(snapshot, key)
    if label is None:
        return None
    chemont_id = label.get("chemont_id")
    return str(chemont_id) if chemont_id is not None else None


def _parent_levels(snapshot: dict[str, object]) -> list[str]:
    direct_name = _label_name(snapshot, "direct_parent")
    direct_id = _label_chemont_id(snapshot, "direct_parent")
    if not direct_name:
        return ["", "", ""]
    if not direct_id:
        return [direct_name, "", ""]

    from apps.chemont.models import ChemOntNode

    stop_ids = {
        _label_chemont_id(snapshot, "subclass"),
        _label_chemont_id(snapshot, "class"),
        _label_chemont_id(snapshot, "superclass"),
        _label_chemont_id(snapshot, "kingdom"),
    }
    stop_ids.discard(None)

    node = (
        ChemOntNode.objects.filter(chemont_id=direct_id)
        .select_related("parent")
        .first()
    )
    if node is None:
        return [direct_name, "", ""]

    chain: list[str] = []
    current: ChemOntNode | None = node
    while current is not None:
        chain.append(current.name)
        if current.chemont_id in stop_ids:
            break
        current = current.parent

    if chain and current is not None and current.chemont_id in stop_ids:
        chain = chain[:-1]
    if not chain:
        chain = [direct_name]

    levels = list(reversed(chain))[:3]
    return [*levels, "", "", ""][:3]


def _event_to_snapshot(event: ClassificationResult) -> dict[str, object]:
    """Adapt a pinned immutable event into the export snapshot label shape.

    The event's ``labels`` JSONB already stores each ChemOnt slot as
    ``{"chemont_id": ..., "name": ...}`` — the exact shape the export consumes —
    so the pinned event is byte-stable provenance with no live re-query.
    """
    labels = event.labels if isinstance(event.labels, dict) else {}
    return {
        "kingdom": labels.get("kingdom"),
        "superclass": labels.get("superclass"),
        "class": labels.get("class"),
        "subclass": labels.get("subclass"),
        "direct_parent": labels.get("direct_parent"),
    }


def _build_row_dict(item: BatchItem) -> dict[str, str | int | None]:
    """Build a flat export row dict for one BatchItem.

    Reads ONLY pinned provenance: the immutable ``resolved_classification`` event
    (new source of truth) or, failing that, the pinned ``classification_snapshot``
    JSON. NEVER re-queries the live cache, so a row stays byte-stable after live
    is later superseded (F-04/F-05).

    Audit columns (Gate-2 WS-E / F-12):
    - Canonical SMILES / InChI from item.compound (CompoundStructure)
    - Requested / returned InChIKey from resolved_classification event
    - Remote query ID / classification version from the same pinned event
    """
    event = item.resolved_classification
    if event is not None:
        snapshot: dict[str, object] | None = _event_to_snapshot(event)
    else:
        snapshot = item.classification_snapshot

    completed = item.status in _FOUND_STATUSES and snapshot is not None
    row: dict[str, str | int | None] = {
        "InChIKey": item.inchikey,
        "ClassyFy Status": "Completed" if completed else "Failed",
        "Kingdom": None,
        "Superclass": None,
        "Class": None,
        "Subclass": None,
        "Parent Level 1": None,
        "Parent Level 2": None,
        "Parent Level 3": None,
        # Audit columns — default to None/empty
        "Canonical SMILES": None,
        "InChI": None,
        "Requested InChIKey": None,
        "Returned InChIKey": None,
        "Remote Query ID": None,
        "Classification Version": None,
    }

    if completed and isinstance(snapshot, dict):
        row["Kingdom"] = _label_name(snapshot, "kingdom")
        row["Superclass"] = _label_name(snapshot, "superclass")
        row["Class"] = _label_name(snapshot, "class")
        row["Subclass"] = _label_name(snapshot, "subclass")
        parent_1, parent_2, parent_3 = _parent_levels(snapshot)
        row["Parent Level 1"] = parent_1
        row["Parent Level 2"] = parent_2
        row["Parent Level 3"] = parent_3

    # Audit: compound structure (canonical SMILES / InChI)
    compound = item.compound
    if compound is not None:
        row["Canonical SMILES"] = compound.canonical_smiles
        row["InChI"] = compound.inchi

    # Audit: pinned event provenance
    if event is not None:
        row["Requested InChIKey"] = event.requested_inchikey
        row["Returned InChIKey"] = event.returned_inchikey
        row["Remote Query ID"] = event.remote_query_id
        row["Classification Version"] = event.classification_version

    return row


def _iter_rows_for_job(job: BatchJob) -> Iterator[dict[str, str | int | None]]:
    """Yield export row dicts for a job, ordered by row_order, using .iterator().

    Memory-bounded: never materialises the full queryset.  Uses select_related
    to avoid N+1 queries for resolved_classification and compound FKs.
    """
    qs = (
        BatchItem.objects.filter(job=job)
        .select_related("resolved_classification", "compound")
        .order_by("row_order")
    )
    for item in qs.iterator(chunk_size=2000):
        yield _build_row_dict(item)


# ---------------------------------------------------------------------------
# Streaming file writers
# ---------------------------------------------------------------------------

class _RowsSignatureHasher:
    """Incrementally hash row data for dedup signature, matching _rows_signature."""

    def __init__(self) -> None:
        self._hasher = hashlib.sha256()
        self._first = True
        # We need to produce the same hash as _rows_signature, which serialises
        # the full payload list.  We replicate that encoding incrementally by
        # hashing a JSON array built piece by piece.
        self._hasher.update(b"[")

    def update(self, row: dict[str, str | int | None]) -> None:
        d = {col: row.get(col) for col in COLUMNS}
        item_bytes = json.dumps(
            d,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if not self._first:
            self._hasher.update(b",")
        self._hasher.update(item_bytes)
        self._first = False

    def hexdigest(self) -> str:
        # Clone to avoid mutating state (so we can call hexdigest multiple times).
        h = self._hasher.copy()
        h.update(b"]")
        return h.hexdigest()


def _write_export_to_temp(
    job: BatchJob,
    fmt: str,
    token: str,
) -> tuple[Path, str, int, int]:
    """Stream the export to a TEMP file in a SINGLE pass.

    The same pass that writes each row also feeds the incremental signature
    hasher, so the rows_signature is a by-product of the write (no second scan).
    The temp file is NOT yet renamed into place; the caller decides whether to
    finalize (:func:`_finalize_temp`) or discard it (dedup hit).

    Returns ``(tmp_path, rows_signature, row_count, byte_size)``.
    """
    storage = _artifact_storage()

    sig_hasher = _RowsSignatureHasher()
    row_count = 0
    tmp_path: Path | None = None

    try:
        with storage.open_atomic_write(token) as writer:
            tmp_path = writer.path
            raw_fh = writer.file
            if fmt in ("csv.gz", "tsv.gz"):
                delimiter = "," if fmt == "csv.gz" else "\t"
                with gzip.GzipFile(fileobj=raw_fh, mode="wb") as gz_fh:
                    gz_text = io.TextIOWrapper(cast(io.BufferedWriter, gz_fh), encoding="utf-8")
                    gz_text.write(CITATION_TEXT)
                    gz_writer = csv.DictWriter(
                        gz_text,
                        fieldnames=COLUMNS,
                        delimiter=delimiter,
                        lineterminator="\n",
                        extrasaction="ignore",
                    )
                    gz_writer.writeheader()
                    for row in _iter_rows_for_job(job):
                        sig_hasher.update(row)
                        escaped = {col: _escape_cell(row.get(col)) for col in COLUMNS}
                        gz_writer.writerow(escaped)
                        row_count += 1
                    gz_text.flush()
                    gz_text.detach()  # prevent TextIOWrapper from closing gz_fh again

            elif fmt in ("csv", "tsv"):
                delimiter = "," if fmt == "csv" else "\t"
                plain_text = io.TextIOWrapper(raw_fh, encoding="utf-8")
                plain_text.write(CITATION_TEXT)
                plain_writer = csv.DictWriter(
                    plain_text,
                    fieldnames=COLUMNS,
                    delimiter=delimiter,
                    lineterminator="\n",
                    extrasaction="ignore",
                )
                plain_writer.writeheader()
                for row in _iter_rows_for_job(job):
                    sig_hasher.update(row)
                    escaped = {col: _escape_cell(row.get(col)) for col in COLUMNS}
                    plain_writer.writerow(escaped)
                    row_count += 1
                plain_text.flush()
                plain_text.detach()

            elif fmt == "json":
                json_text = io.TextIOWrapper(raw_fh, encoding="utf-8")
                json_text.write("[")
                first = True
                for row in _iter_rows_for_job(job):
                    sig_hasher.update(row)
                    d: dict[str, object] = {"_citation": CITATION_DICT}
                    for col in COLUMNS:
                        d[col] = row.get(col)
                    entry = json.dumps(d, ensure_ascii=False, default=str)
                    if not first:
                        json_text.write(",")
                    json_text.write(entry)
                    first = False
                    row_count += 1
                json_text.write("]")
                json_text.flush()
                json_text.detach()

            else:
                raise ValueError(f"Unsupported export format: {fmt!r}")

        if tmp_path is None:
            raise RuntimeError("Artifact storage did not provide a temp path")

        rows_signature = sig_hasher.hexdigest()
        byte_size = storage.stat(tmp_path).byte_size
        return tmp_path, rows_signature, row_count, byte_size

    except Exception:
        if tmp_path is not None:
            _discard_temp(tmp_path)
        raise


def _finalize_temp(tmp_path: Path, token: str, fmt: str) -> Path:
    """Atomically rename a finished temp file into its final EXPORT_DIR slot."""
    return _artifact_storage().commit_atomic_write(tmp_path, token, fmt)


def _discard_temp(tmp_path: Path) -> None:
    """Remove a temp file (dedup hit or error path); best-effort."""
    try:
        _artifact_storage().delete(tmp_path)
    except OSError:
        pass


def _checksum_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    return _artifact_storage().checksum(path)


# ---------------------------------------------------------------------------
# Legacy sidecar helpers (kept for backward compatibility)
# ---------------------------------------------------------------------------

def _meta_path(token: str) -> Path:
    return _artifact_storage().meta_path(token)


def _load_meta(token: str) -> dict[str, str] | None:
    """Load metadata — first from DB (ExportArtifact), then sidecar fallback."""
    artifact = ExportArtifact.objects.filter(token=token).first()
    if artifact is not None:
        return {
            "token": artifact.token,
            "job_id": str(artifact.job_id),
            "fmt": artifact.fmt,
            "expires_at": artifact.expires_at.isoformat(),
            "rows_signature": artifact.rows_signature,
            "checksum": artifact.checksum,
            "byte_size": str(artifact.byte_size),
            "row_count": str(artifact.row_count),
        }

    # Fallback: legacy JSON sidecar (for tests that write it directly)
    storage = _artifact_storage()
    p = storage.meta_path(token)
    if not storage.exists(p):
        return None
    try:
        with storage.open(p, "rb") as fh:
            result = json.loads(fh.read().decode("utf-8"))
        if isinstance(result, dict):
            return {str(k): str(v) for k, v in result.items()}
        return None
    except Exception:
        return None


def _is_valid_meta(meta: dict[str, str]) -> bool:
    """Return True if the meta is not expired."""
    try:
        expires_at = datetime.fromisoformat(meta["expires_at"])
        return expires_at > datetime.now(tz=dt.UTC)
    except (KeyError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Core export logic
# ---------------------------------------------------------------------------

def _find_existing_artifact(job_id: str, fmt: str, rows_signature: str) -> ExportArtifact | None:
    """Return a valid ExportArtifact for job+format+signature, or None."""
    now = timezone.now()
    artifact = (
        ExportArtifact.objects.filter(
            job_id=uuid.UUID(job_id),
            fmt=fmt,
            rows_signature=rows_signature,
            expires_at__gt=now,
        )
        .first()
    )
    if artifact is None:
        return None
    # Verify the export file still exists
    if not _artifact_storage().exists(Path(artifact.path)):
        return None
    return artifact


def _matches_current_rows_signature(meta: dict[str, str]) -> bool:
    """Return True when metadata still matches the current job export rows.

    This is used by get_export_path to verify the file corresponds to the
    current state of the job (guards against stale tokens after job updates).
    """
    expected_signature = meta.get("rows_signature")
    job_id = meta.get("job_id")
    if not expected_signature or not job_id:
        return False

    job = BatchJob.objects.filter(pk=job_id).first()
    if job is None:
        return False

    # Compute signature via a fresh streaming pass (memory-bounded)
    current_sig = _compute_rows_signature(job)
    return current_sig == expected_signature


def _compute_rows_signature(job: BatchJob) -> str:
    """Compute the rows_signature via a streaming pass (memory-bounded)."""
    sig_hasher = _RowsSignatureHasher()
    qs = (
        BatchItem.objects.filter(job=job)
        .select_related("resolved_classification", "compound")
        .order_by("row_order")
    )
    for item in qs.iterator(chunk_size=2000):
        row = _build_row_dict(item)
        sig_hasher.update(row)
    return sig_hasher.hexdigest()


def _create_or_reuse_export(job: BatchJob, fmt: str) -> tuple[str, str]:
    valid_formats = {"csv", "tsv", "json", "csv.gz", "tsv.gz"}
    if fmt not in valid_formats:
        raise ValueError(f"Unsupported format: {fmt!r}. Must be one of: {valid_formats}")

    token = secrets.token_urlsafe(32)

    # SINGLE streaming pass: write to a temp file and compute the rows_signature
    # at the same time.  We cannot know the signature until we have scanned the
    # rows, so write-to-temp-then-dedup is the correct shape: if an existing
    # valid artifact already has this signature, discard the temp file and reuse
    # its token; otherwise finalize the temp file (atomic rename) and persist.
    tmp_path, rows_signature, row_count, byte_size = _write_export_to_temp(job, fmt, token)

    existing = _find_existing_artifact(str(job.pk), fmt, rows_signature)
    if existing is not None:
        _discard_temp(tmp_path)
        return existing.token, existing.expires_at.isoformat()

    retention_days = getattr(settings, "EXPORT_RETENTION_DAYS", 14)
    expires_at = datetime.now(tz=dt.UTC) + timedelta(days=retention_days)
    expires_at_iso = expires_at.isoformat()

    # Checksum the temp file BEFORE the atomic rename, then finalize.
    checksum = _checksum_file(tmp_path)
    dest = _finalize_temp(tmp_path, token, fmt)

    # Persist to DB (source of truth)
    ExportArtifact.objects.create(
        job=job,
        fmt=fmt,
        token=token,
        path=str(dest),
        checksum=checksum,
        byte_size=byte_size,
        rows_signature=rows_signature,
        row_count=row_count,
        expires_at=expires_at,
    )

    # Also write legacy sidecar for backward compatibility
    meta_dict = {
        "token": token,
        "job_id": str(job.pk),
        "fmt": fmt,
        "expires_at": expires_at_iso,
        "rows_signature": rows_signature,
    }
    meta_bytes = json.dumps(meta_dict).encode("utf-8")
    storage = _artifact_storage()
    storage.write_bytes_atomic(storage.meta_path(token), meta_bytes)

    return token, expires_at_iso


def create_export(job: BatchJob, fmt: str, plaintext_secret: str) -> tuple[str, str]:
    """Create or reuse an export after validating the plaintext job secret."""
    if not check_password(plaintext_secret, job.secret_hash):
        raise PermissionError("Invalid secret")
    return _create_or_reuse_export(job, fmt)


def create_export_for_authorized_job(job: BatchJob, fmt: str) -> tuple[str, str]:
    """Create or reuse an export when the caller already proved job access."""
    return _create_or_reuse_export(job, fmt)


def get_export_metadata(token: str) -> dict[str, str] | None:
    """Return parsed export metadata for auth checks, or None."""
    return _load_meta(token)


def get_export_path(token: str) -> Path | None:
    """Return the file path for a valid export token, or None if not found/expired."""
    storage = _artifact_storage()

    # Primary path: look up ExportArtifact in DB
    artifact = ExportArtifact.objects.filter(token=token).first()
    if artifact is not None:
        now = timezone.now()
        if artifact.expires_at <= now:
            return None
        path = Path(artifact.path)
        if not storage.exists(path):
            return None
        job = BatchJob.objects.filter(pk=artifact.job_id).first()
        if job is None:
            return None
        # Cheap proxy guard: a job whose row count is unchanged from when the
        # artifact was created is overwhelmingly the common case.  Only fall
        # back to the full streaming re-signature (one BatchItem scan) when the
        # cheap count differs — which is the only way row CONTENT can change
        # without changing the count, so we must still verify in that branch.
        current_count = BatchItem.objects.filter(job=job).count()
        if current_count == artifact.row_count:
            # Counts match: verify content signature (rows can mutate in place,
            # e.g. an item resolving, without changing the count).
            current_sig = _compute_rows_signature(job)
            if current_sig != artifact.rows_signature:
                return None
            return path
        # Count changed → definitely stale.
        return None

    # Fallback: legacy sidecar path (for tests that write .meta.json directly)
    meta = _load_meta(token)
    if meta is None or not _is_valid_meta(meta):
        return None
    if not _matches_current_rows_signature(meta):
        return None
    fmt = meta.get("fmt", "")
    path = storage.local_path(token, fmt)
    return path if storage.exists(path) else None


# ---------------------------------------------------------------------------
# Legacy helpers kept for tests that access internals directly
# ---------------------------------------------------------------------------

def _rows_for_job(job: BatchJob) -> list[dict[str, str | int | None]]:
    """Return all export row dicts for a job, ordered by row_order.

    NOTE: This materialises all rows in memory and exists only for backward
    compatibility with tests that call it directly.  New code should use
    _iter_rows_for_job() which is memory-bounded.
    """
    return list(_iter_rows_for_job(job))


def _rows_signature(rows: list[dict[str, str | int | None]]) -> str:
    """Compute rows_signature from a pre-materialised list (legacy interface).

    The result is identical to the incremental hasher used in
    _write_export_to_temp/_compute_rows_signature, so tokens created by
    either path share the same dedup key.
    """
    sig_hasher = _RowsSignatureHasher()
    for row in rows:
        sig_hasher.update(row)
    return sig_hasher.hexdigest()


def _find_existing_token(job_id: str, fmt: str, rows_signature: str) -> str | None:
    """Return an existing valid token for job+format, or None.

    Kept for backward compatibility.  New code uses _find_existing_artifact.
    """
    artifact = _find_existing_artifact(job_id, fmt, rows_signature)
    if artifact is not None:
        return artifact.token
    return None
