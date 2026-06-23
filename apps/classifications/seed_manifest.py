"""Seed manifest helpers: resolve Zenodo record metadata and manage SeedImport rows.

Seed versioning / view design
------------------------------
Each seed import writes its rows into a **versioned physical table**
``classyfire_seed_v<N>`` (where N is the SeedImport PK).  The canonical
read interface is the SQL VIEW ``classyfire_seed_current`` which is atomically
swapped by ``seed_activate`` to point at the newly validated versioned table.

For the LITE/fixture profile (and tests), rows land in the base
``classyfire_seed`` table and ``classyfire_seed_current`` is a view over that.

Checkpoint/resume columns
--------------------------
``SeedImport.audit_result`` (JSONB) stores a ``checkpoint`` key with the last
successfully committed offset (row count), so ``--resume`` can skip rows that
were already loaded.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default Zenodo record ID for the ClassyFire seed dataset.
DEFAULT_RECORD_ID = 20472700
ZENODO_API_BASE = "https://zenodo.org/api/records"

# The view name that application code must always query.
CURRENT_VIEW_NAME = "classyfire_seed_current"


class ManifestError(Exception):
    """Raised for manifest / metadata problems."""


def compute_sha256(path: str | Path, *, chunk_size: int = 1 << 20) -> str:
    """Return the hex SHA-256 digest of *path*."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def resolve_zenodo_record(
    record_id: int | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fetch Zenodo record metadata and return a manifest dict.

    Parameters
    ----------
    record_id:
        Zenodo record ID.  Defaults to ``SEED_RECORD_ID`` setting or
        ``DEFAULT_RECORD_ID``.
    dry_run:
        If ``True``, skip the network call and return a placeholder manifest.
        This is the offline/test mode.

    Returns
    -------
    Dict with keys: ``record_id``, ``doi``, ``filename``, ``url``,
    ``checksum``, ``license``, ``expected_row_count`` (None unless
    available in metadata).
    """
    if record_id is None:
        try:
            from django.conf import settings

            record_id = getattr(settings, "SEED_RECORD_ID", DEFAULT_RECORD_ID)
        except Exception:
            record_id = DEFAULT_RECORD_ID

    if dry_run:
        return {
            "record_id": str(record_id),
            "doi": f"10.5281/zenodo.{record_id}",
            "filename": "classyfire_seed.tsv.zst",
            "url": f"{ZENODO_API_BASE}/{record_id}/files/classyfire_seed.tsv.zst",
            "checksum": None,
            "license": "CC-BY-4.0",
            "expected_row_count": None,
            "dry_run": True,
        }

    # Real network fetch.
    try:
        import httpx

        url = f"{ZENODO_API_BASE}/{record_id}"
        resp = httpx.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise ManifestError(f"Failed to fetch Zenodo record {record_id}: {exc}") from exc

    doi = data.get("doi", f"10.5281/zenodo.{record_id}")
    license_info = data.get("metadata", {}).get("license", {})
    if isinstance(license_info, dict):
        license_id = license_info.get("id", "")
    else:
        license_id = str(license_info)

    # Find the main TSV.zst file.
    files = data.get("files", [])
    tsv_file: dict[str, Any] | None = None
    for f in files:
        key = f.get("key", "") or f.get("filename", "")
        if key.endswith(".tsv.zst"):
            tsv_file = f
            break
    if tsv_file is None:
        raise ManifestError(f"No .tsv.zst file found in Zenodo record {record_id}")

    key = tsv_file.get("key", "") or tsv_file.get("filename", "")
    checksum_raw = tsv_file.get("checksum", "")
    # Zenodo checksums are "md5:..." or "sha256:..." prefixed.
    checksum = checksum_raw.split(":", 1)[-1] if ":" in checksum_raw else checksum_raw
    file_url = tsv_file.get("links", {}).get("self", "")

    return {
        "record_id": str(record_id),
        "doi": doi,
        "filename": key,
        "url": file_url,
        "checksum": checksum,
        "license": license_id,
        "expected_row_count": None,
        "dry_run": False,
    }


def create_seed_import_row(manifest: dict[str, Any]) -> Any:
    """Create a ``SeedImport`` DB row from a resolved *manifest* dict.

    Returns the saved ``SeedImport`` instance.
    """
    from apps.operations.models import SeedImport

    obj = SeedImport.objects.create(
        doi=manifest.get("doi"),
        record_id=manifest.get("record_id"),
        filename=manifest.get("filename"),
        checksum=manifest.get("checksum"),
        license=manifest.get("license"),
        expected_row_count=manifest.get("expected_row_count"),
        import_status="PENDING",
        who=os.environ.get("USER", ""),
    )
    logger.info("Created SeedImport row pk=%s", obj.pk)
    return obj


def versioned_table_name(seed_import_pk: int) -> str:
    """Return the physical versioned table name for *seed_import_pk*."""
    return f"classyfire_seed_v{seed_import_pk}"
