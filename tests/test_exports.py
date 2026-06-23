"""Tests for the batch export functionality (Task 8).

Tests cover:
- CSV/TSV/JSON/CSV.gz/TSV.gz export formats
- Column order and presence
- Row ordering and deduplication
- Citation metadata
- Injection escaping (FR-028)
- Token-based download via API
- Export deduplication (same job+format → same token)
"""
from __future__ import annotations

import csv
import gzip
import json

import pytest
from django.test import Client

from apps.batches.ingest import parse_text_input
from apps.batches.services import create_job

ASPIRIN_SMILES = "CC(=O)Oc1ccccc1C(=O)O"
CAFFEINE_SMILES = "Cn1cnc2c1c(=O)n(c(=O)n2C)C"

EXPECTED_COLUMNS = [
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


def _create_simple_job():
    """Create a job with one ASPIRIN row."""
    rows = list(parse_text_input(ASPIRIN_SMILES))
    return create_job(rows, remote_enabled=False)


# ---------------------------------------------------------------------------
# Helper: build an export via API and return raw bytes
# ---------------------------------------------------------------------------


def _get_export_bytes(client: Client, job_id: str, secret: str, fmt: str) -> bytes:
    resp = client.post(
        f"/api/jobs/{job_id}/exports",
        data=json.dumps({"format": fmt, "secret": secret}),
        content_type="application/json",
    )
    assert resp.status_code == 201, f"Export creation failed: {resp.status_code} {resp.content}"
    token = resp.json()["token"]
    dl_resp = client.get(f"/api/exports/{token}")
    assert dl_resp.status_code == 200, f"Download failed: {dl_resp.status_code}"
    # FileResponse uses streaming_content; regular responses use content
    if hasattr(dl_resp, "streaming_content"):
        return b"".join(dl_resp.streaming_content)
    return dl_resp.content


# ---------------------------------------------------------------------------
# 1. CSV export has correct columns
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_csv_export_columns():
    job, secret = _create_simple_job()
    client = Client()
    raw = _get_export_bytes(client, str(job.pk), secret, "csv")
    text = raw.decode("utf-8")
    # Skip comment lines
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    reader = csv.DictReader(lines)
    fieldnames = reader.fieldnames or []
    for col in EXPECTED_COLUMNS:
        assert col in fieldnames, f"Column '{col}' missing from CSV"


# ---------------------------------------------------------------------------
# 2. TSV uses tabs not commas
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_tsv_export_tab_separated():
    job, secret = _create_simple_job()
    client = Client()
    raw = _get_export_bytes(client, str(job.pk), secret, "tsv")
    text = raw.decode("utf-8")
    data_lines = [line for line in text.splitlines() if not line.startswith("#")]
    assert len(data_lines) >= 1
    # Header line should have tabs
    header = data_lines[0]
    assert "\t" in header, "TSV header should contain tabs"
    assert "," not in header, "TSV header should not contain commas"


# ---------------------------------------------------------------------------
# 3. JSON export is list of dicts with all fields
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_json_export_list_of_dicts():
    job, secret = _create_simple_job()
    client = Client()
    raw = _get_export_bytes(client, str(job.pk), secret, "json")
    data = json.loads(raw.decode("utf-8"))
    assert isinstance(data, list), "JSON export should be a list"
    assert len(data) == 1
    row = data[0]
    assert isinstance(row, dict)
    for col in EXPECTED_COLUMNS:
        assert col in row, f"Field '{col}' missing from JSON row"


# ---------------------------------------------------------------------------
# 4. CSV.gz decompresses to valid CSV
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_csv_gz_export_decompresses_to_valid_csv():
    job, secret = _create_simple_job()
    client = Client()
    raw = _get_export_bytes(client, str(job.pk), secret, "csv.gz")
    text = gzip.decompress(raw).decode("utf-8")
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    reader = csv.DictReader(lines)
    fieldnames = reader.fieldnames or []
    assert "row_order" in fieldnames
    assert "inchikey" in fieldnames


# ---------------------------------------------------------------------------
# 5. TSV.gz decompresses to valid TSV
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_tsv_gz_export_decompresses_to_valid_tsv():
    job, secret = _create_simple_job()
    client = Client()
    raw = _get_export_bytes(client, str(job.pk), secret, "tsv.gz")
    text = gzip.decompress(raw).decode("utf-8")
    data_lines = [line for line in text.splitlines() if not line.startswith("#")]
    assert len(data_lines) >= 1
    header = data_lines[0]
    assert "\t" in header


# ---------------------------------------------------------------------------
# 6. Row order preserved
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_row_order_preserved():
    text = f"{ASPIRIN_SMILES}\n{CAFFEINE_SMILES}"
    rows = list(parse_text_input(text))
    job, secret = create_job(rows, remote_enabled=False)
    client = Client()
    raw = _get_export_bytes(client, str(job.pk), secret, "csv")
    text_out = raw.decode("utf-8")
    lines = [line for line in text_out.splitlines() if not line.startswith("#")]
    reader = csv.DictReader(lines)
    data_rows = list(reader)
    assert len(data_rows) == 2
    assert int(data_rows[0]["row_order"]) == 0
    assert int(data_rows[1]["row_order"]) == 1


# ---------------------------------------------------------------------------
# 7. Duplicate rows preserved
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_duplicate_rows_preserved():
    text = f"{ASPIRIN_SMILES}\n{ASPIRIN_SMILES}"
    rows = list(parse_text_input(text))
    job, secret = create_job(rows, remote_enabled=False)
    client = Client()
    raw = _get_export_bytes(client, str(job.pk), secret, "csv")
    text_out = raw.decode("utf-8")
    lines = [line for line in text_out.splitlines() if not line.startswith("#")]
    reader = csv.DictReader(lines)
    data_rows = list(reader)
    assert len(data_rows) == 2, f"Expected 2 rows (duplicates preserved), got {len(data_rows)}"


# ---------------------------------------------------------------------------
# 8. Source fields present
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_source_fields_present():
    job, secret = _create_simple_job()
    client = Client()
    raw = _get_export_bytes(client, str(job.pk), secret, "csv")
    text_out = raw.decode("utf-8")
    lines = [line for line in text_out.splitlines() if not line.startswith("#")]
    reader = csv.DictReader(lines)
    fieldnames = reader.fieldnames or []
    for col in ["source_type", "source_ref", "classification_version", "retrieved_at"]:
        assert col in fieldnames, f"Source field '{col}' missing"


# ---------------------------------------------------------------------------
# 9. Citation metadata in CSV (comment header)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_citation_metadata_csv():
    job, secret = _create_simple_job()
    client = Client()
    raw = _get_export_bytes(client, str(job.pk), secret, "csv")
    text = raw.decode("utf-8")
    assert text.startswith("# ClassyFire"), (
        f"CSV should start with citation header, got: {text[:100]!r}"
    )


# ---------------------------------------------------------------------------
# 10. Citation metadata in JSON (_citation key)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_citation_metadata_json():
    job, secret = _create_simple_job()
    client = Client()
    raw = _get_export_bytes(client, str(job.pk), secret, "json")
    data = json.loads(raw.decode("utf-8"))
    assert len(data) >= 1
    row = data[0]
    assert "_citation" in row, "JSON row should have '_citation' key"


# ---------------------------------------------------------------------------
# 11. Injection escaping (FR-028)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_injection_escaping():
    rows = [{"raw": ASPIRIN_SMILES, "row_index": 0, "external_id": "=1+1"}]
    job, secret = create_job(rows, remote_enabled=False)
    client = Client()
    raw = _get_export_bytes(client, str(job.pk), secret, "csv")
    text = raw.decode("utf-8")
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    reader = csv.DictReader(lines)
    data_rows = list(reader)
    assert len(data_rows) == 1
    ext_id = data_rows[0]["external_id"]
    assert ext_id == "'=1+1", f"Expected injection-escaped value, got {ext_id!r}"


# ---------------------------------------------------------------------------
# 12. Export token download flow
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_export_token_download():
    job, secret = _create_simple_job()
    client = Client()
    # Create export
    resp = client.post(
        f"/api/jobs/{job.pk}/exports",
        data=json.dumps({"format": "csv", "secret": secret}),
        content_type="application/json",
    )
    assert resp.status_code == 201
    payload = resp.json()
    assert "token" in payload
    assert "expires_at" in payload
    token = payload["token"]
    # Download
    dl = client.get(f"/api/exports/{token}")
    assert dl.status_code == 200
    raw = b"".join(dl.streaming_content) if hasattr(dl, "streaming_content") else dl.content
    content = raw.decode("utf-8")
    # Should be a valid CSV with our header
    assert "row_order" in content


# ---------------------------------------------------------------------------
# 13. Export deduplication — same job+format → same token
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_export_deduplication():
    job, secret = _create_simple_job()
    client = Client()
    body = json.dumps({"format": "csv", "secret": secret})
    resp1 = client.post(
        f"/api/jobs/{job.pk}/exports",
        data=body,
        content_type="application/json",
    )
    resp2 = client.post(
        f"/api/jobs/{job.pk}/exports",
        data=body,
        content_type="application/json",
    )
    assert resp1.status_code == 201
    assert resp2.status_code == 201
    token1 = resp1.json()["token"]
    token2 = resp2.json()["token"]
    assert token1 == token2, "Same job+format should return the same token (dedup)"
