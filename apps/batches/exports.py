"""Export functionality for batch job results.

Public interface
----------------
create_export(job, fmt, secret) -> (token, expires_at)
    Create (or reuse) an export file for the given job and format.
    Validates secret. Returns a URL-safe token and expiry datetime.

get_export_path(token) -> Path | None
    Return the file path for a valid, non-expired export token, or None.

EXPORT_DIR: Path — directory where export files are stored.

Formats supported: csv, tsv, json, csv.gz, tsv.gz

Citation header (FR-028): prepended to CSV/TSV formats as # comment lines.
Injection escaping (FR-028): cells starting with =, +, -, @ are prefixed with '.
Token expiry: settings.EXPORT_RETENTION_DAYS days from creation.
Deduplication: same job+format with valid non-expired token returns same token.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth.hashers import check_password

from apps.batches.models import BatchItem, BatchJob
from apps.classifications.services import lookup_current_classification

if TYPE_CHECKING:
    pass

EXPORT_DIR: Path = settings.BASE_DIR / "export_cache"

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
    "row_order",
    "external_id",
    "original_input",
    "detected_type",
    "inchikey",
    "status",
    "message",
    "kingdom_name",
    "kingdom_chemont_id",
    "superclass_name",
    "superclass_chemont_id",
    "class_name",
    "class_chemont_id",
    "subclass_name",
    "subclass_chemont_id",
    "direct_parent_name",
    "direct_parent_chemont_id",
    "source_type",
    "source_priority",
    "source_ref",
    "classification_version",
    "retrieved_at",
]

# Statuses that warrant a classification lookup
_FOUND_STATUSES = frozenset([
    "FOUND_LIVE_CACHE",
    "FOUND_SEED_CACHE",
    "FOUND_WISHART_ENTITY",
    "COMPLETE_AFTER_WISHART",
])


def _escape_cell(value: str | None) -> str:
    """FR-028: Escape CSV/TSV cell values to prevent formula injection."""
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in ("=", "+", "-", "@"):
        return "'" + s
    if "\t" in s or "\r" in s:
        return "'" + s
    return s


def _build_row_dict(item: BatchItem) -> dict[str, str | int | None]:
    """Build a flat export row dict for one BatchItem."""
    row: dict[str, str | int | None] = {
        "row_order": item.row_order,
        "external_id": item.external_id,
        "original_input": item.original_input,
        "detected_type": item.detected_type,
        "inchikey": item.inchikey,
        "status": item.status,
        "message": item.message,
        "kingdom_name": None,
        "kingdom_chemont_id": None,
        "superclass_name": None,
        "superclass_chemont_id": None,
        "class_name": None,
        "class_chemont_id": None,
        "subclass_name": None,
        "subclass_chemont_id": None,
        "direct_parent_name": None,
        "direct_parent_chemont_id": None,
        "source_type": None,
        "source_priority": None,
        "source_ref": None,
        "classification_version": None,
        "retrieved_at": None,
    }

    if item.status in _FOUND_STATUSES and item.inchikey:
        result = lookup_current_classification(item.inchikey)
        if result is not None:
            if result.kingdom:
                row["kingdom_name"] = result.kingdom.name
                row["kingdom_chemont_id"] = result.kingdom.chemont_id
            if result.superclass:
                row["superclass_name"] = result.superclass.name
                row["superclass_chemont_id"] = result.superclass.chemont_id
            if result.class_:
                row["class_name"] = result.class_.name
                row["class_chemont_id"] = result.class_.chemont_id
            if result.subclass:
                row["subclass_name"] = result.subclass.name
                row["subclass_chemont_id"] = result.subclass.chemont_id
            if result.direct_parent:
                row["direct_parent_name"] = result.direct_parent.name
                row["direct_parent_chemont_id"] = result.direct_parent.chemont_id
            row["source_type"] = result.source_type
            row["source_priority"] = result.source_priority
            row["source_ref"] = result.source_ref
            row["classification_version"] = result.classification_version
            row["retrieved_at"] = (
                result.retrieved_at.isoformat() if result.retrieved_at else None
            )

    return row


def _rows_for_job(job: BatchJob) -> list[dict[str, str | int | None]]:
    """Return all export row dicts for a job, ordered by row_order."""
    items = BatchItem.objects.filter(job=job).order_by("row_order")
    return [_build_row_dict(item) for item in items]


def _generate_csv_bytes(rows: list[dict[str, str | int | None]], delimiter: str = ",") -> bytes:
    """Generate UTF-8 CSV or TSV bytes with citation header and injection escaping."""
    buf = io.StringIO()
    buf.write(CITATION_TEXT)
    writer = csv.DictWriter(
        buf,
        fieldnames=COLUMNS,
        delimiter=delimiter,
        lineterminator="\n",
        extrasaction="ignore",
    )
    writer.writeheader()
    for row in rows:
        escaped = {col: _escape_cell(row.get(col)) for col in COLUMNS}  # type: ignore[arg-type]
        writer.writerow(escaped)
    return buf.getvalue().encode("utf-8")


def _generate_json_bytes(rows: list[dict[str, str | int | None]]) -> bytes:
    """Generate JSON bytes (list of dicts) with _citation key in each row."""
    output = []
    for row in rows:
        d: dict[str, object] = {"_citation": CITATION_DICT}
        for col in COLUMNS:
            d[col] = row.get(col)
        output.append(d)
    return json.dumps(output, ensure_ascii=False, default=str).encode("utf-8")


def _write_export_file(job: BatchJob, fmt: str, token: str) -> Path:
    """Generate the export file and write it to EXPORT_DIR/{token}.{fmt}."""
    EXPORT_DIR.mkdir(exist_ok=True)
    rows = _rows_for_job(job)

    ext = fmt  # csv, tsv, json, csv.gz, tsv.gz
    dest = EXPORT_DIR / f"{token}.{ext}"

    if fmt == "csv":
        dest.write_bytes(_generate_csv_bytes(rows, delimiter=","))
    elif fmt == "tsv":
        dest.write_bytes(_generate_csv_bytes(rows, delimiter="\t"))
    elif fmt == "json":
        dest.write_bytes(_generate_json_bytes(rows))
    elif fmt == "csv.gz":
        dest.write_bytes(gzip.compress(_generate_csv_bytes(rows, delimiter=",")))
    elif fmt == "tsv.gz":
        dest.write_bytes(gzip.compress(_generate_csv_bytes(rows, delimiter="\t")))
    else:
        raise ValueError(f"Unsupported export format: {fmt!r}")

    return dest


def _meta_path(token: str) -> Path:
    return EXPORT_DIR / f"{token}.meta.json"


def _load_meta(token: str) -> dict[str, str] | None:
    p = _meta_path(token)
    if not p.exists():
        return None
    try:
        result = json.loads(p.read_text())
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


def _find_existing_token(job_id: str, fmt: str) -> str | None:
    """Return an existing valid token for job+format, or None."""
    EXPORT_DIR.mkdir(exist_ok=True)
    for meta_file in EXPORT_DIR.glob("*.meta.json"):
        try:
            raw = json.loads(meta_file.read_text())
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        parsed: dict[str, str] = {str(k): str(v) for k, v in raw.items()}
        if parsed.get("job_id") == job_id and parsed.get("fmt") == fmt and _is_valid_meta(parsed):
            token = parsed.get("token")
            if token:
                # Verify export file still exists
                ext_path = EXPORT_DIR / f"{token}.{fmt}"
                if ext_path.exists():
                    return token
    return None


def create_export(job: BatchJob, fmt: str, plaintext_secret: str) -> tuple[str, str]:
    """Create or reuse an export for *job* in *fmt*.

    Parameters
    ----------
    job:
        The BatchJob to export.
    fmt:
        One of: csv, tsv, json, csv.gz, tsv.gz.
    plaintext_secret:
        The plaintext job secret for authentication.

    Returns
    -------
    (token, expires_at_iso)
        Token is URL-safe and can be used to retrieve the file.

    Raises
    ------
    PermissionError if secret doesn't match.
    ValueError if fmt is unsupported.
    """
    if not check_password(plaintext_secret, job.secret_hash):
        raise PermissionError("Invalid secret")

    valid_formats = {"csv", "tsv", "json", "csv.gz", "tsv.gz"}
    if fmt not in valid_formats:
        raise ValueError(f"Unsupported format: {fmt!r}. Must be one of: {valid_formats}")

    # Dedup: reuse existing valid token if available
    existing = _find_existing_token(str(job.pk), fmt)
    if existing:
        meta = _load_meta(existing)
        if meta:
            return existing, meta["expires_at"]

    # Create new export
    token = secrets.token_urlsafe(32)
    retention_days = getattr(settings, "EXPORT_RETENTION_DAYS", 14)
    expires_at = datetime.now(tz=dt.UTC) + timedelta(days=retention_days)
    expires_at_iso = expires_at.isoformat()

    EXPORT_DIR.mkdir(exist_ok=True)
    _write_export_file(job, fmt, token)

    meta = {
        "token": token,
        "job_id": str(job.pk),
        "fmt": fmt,
        "expires_at": expires_at_iso,
    }
    _meta_path(token).write_text(json.dumps(meta))

    return token, expires_at_iso


def get_export_path(token: str) -> Path | None:
    """Return the file path for a valid export token, or None if not found/expired."""
    meta = _load_meta(token)
    if meta is None or not _is_valid_meta(meta):
        return None
    fmt = meta.get("fmt", "")
    path = EXPORT_DIR / f"{token}.{fmt}"
    return path if path.exists() else None
