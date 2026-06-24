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
from apps.classifications.services import snapshot_current_classification

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
    "InChIKey",
    "ClassyFy Status",
    "Kingdom",
    "Superclass",
    "Class",
    "Subclass",
    "Parent Level 1",
    "Parent Level 2",
    "Parent Level 3",
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


def _build_row_dict(item: BatchItem) -> dict[str, str | int | None]:
    """Build a flat export row dict for one BatchItem."""
    snapshot = item.classification_snapshot
    if snapshot is None and item.status in _FOUND_STATUSES and item.inchikey:
        snapshot = snapshot_current_classification(item.inchikey)

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
