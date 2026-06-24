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
ASPIRIN_INCHIKEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
CAFFEINE_SMILES = "Cn1cnc2c1c(=O)n(c(=O)n2C)C"

EXPECTED_COLUMNS = [
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


def _create_simple_job():
    """Create a job with one ASPIRIN row."""
    rows = list(parse_text_input(ASPIRIN_SMILES))
    return create_job(rows, remote_enabled=False)


def _make_chemont(
    numeric_id: int,
    name: str,
    *,
    parent_id: int | None = None,
) -> object:
    from apps.chemont.models import ChemOntNode

    return ChemOntNode.objects.create(
        numeric_id=numeric_id,
        chemont_id=f"CHEMONT:{numeric_id:07d}",
        name=name,
        parent_id=parent_id,
        ontology_version="2.1",
    )


def _make_live(
    inchikey: str,
    *,
    kingdom_id: int | None = None,
    superclass_id: int | None = None,
    class_id: int | None = None,
    subclass_id: int | None = None,
    direct_parent_id: int | None = None,
    version: str = "v1",
) -> object:
    import datetime as dt

    from apps.classifications.models import ClassyfireLive

    return ClassyfireLive.objects.create(
        inchikey=inchikey,
        source_type="WISHART_LIVE_QUERY",
        source_priority=100,
        retrieved_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        classification_version=version,
        kingdom_id=kingdom_id,
        superclass_id=superclass_id,
        class_node_id=class_id,
        subclass_id=subclass_id,
        direct_parent_id=direct_parent_id,
    )


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


def _create_export_token(client: Client, job_id: str, secret: str, fmt: str) -> str:
    resp = client.post(
        f"/api/jobs/{job_id}/exports",
        data=json.dumps({"format": fmt, "secret": secret}),
        content_type="application/json",
    )
    assert resp.status_code == 201, f"Export creation failed: {resp.status_code} {resp.content}"
    return str(resp.json()["token"])


def _download_export_bytes(client: Client, token: str) -> bytes:
    dl_resp = client.get(f"/api/exports/{token}")
    assert dl_resp.status_code == 200, f"Download failed: {dl_resp.status_code}"
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


@pytest.mark.django_db(transaction=True)
def test_csv_export_requested_classyfy_row_shape():
    kingdom = _make_chemont(1000, "Organic compounds")
    superclass = _make_chemont(
        1001, "Lipids and lipid-like molecules", parent_id=kingdom.numeric_id
    )
    klass = _make_chemont(1002, "Fatty Acyls", parent_id=superclass.numeric_id)
    subclass = _make_chemont(1003, "Fatty acid esters", parent_id=klass.numeric_id)
    parent = _make_chemont(1004, "Acyl carnitines", parent_id=subclass.numeric_id)
    _make_live(
        ASPIRIN_INCHIKEY,
        kingdom_id=kingdom.numeric_id,
        superclass_id=superclass.numeric_id,
        class_id=klass.numeric_id,
        subclass_id=subclass.numeric_id,
        direct_parent_id=parent.numeric_id,
    )

    job, secret = _create_simple_job()
    client = Client()
    raw = _get_export_bytes(client, str(job.pk), secret, "csv")
    text = raw.decode("utf-8")
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    row = next(csv.DictReader(lines))

    assert row == {
        "InChIKey": ASPIRIN_INCHIKEY,
        "ClassyFy Status": "Completed",
        "Kingdom": "Organic compounds",
        "Superclass": "Lipids and lipid-like molecules",
        "Class": "Fatty Acyls",
        "Subclass": "Fatty acid esters",
        "Parent Level 1": "Acyl carnitines",
        "Parent Level 2": "",
        "Parent Level 3": "",
    }


@pytest.mark.django_db(transaction=True)
def test_batch_item_and_export_keep_original_classification_after_live_supersede():
    original = _make_chemont(1100, "Original kingdom")
    replacement = _make_chemont(1101, "Replacement kingdom")
    _make_live(ASPIRIN_INCHIKEY, kingdom_id=original.numeric_id, version="v1")

    job, secret = _create_simple_job()
    item = job.items.get()
    assert item.classification_snapshot is not None
    assert item.classification_snapshot["kingdom"]["name"] == "Original kingdom"

    from apps.classifications.models import ClassyfireLive

    ClassyfireLive.objects.filter(inchikey=ASPIRIN_INCHIKEY).update(
        kingdom_id=replacement.numeric_id,
        classification_version="v2",
    )

    client = Client()
    raw = _get_export_bytes(client, str(job.pk), secret, "csv")
    text = raw.decode("utf-8")
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    row = next(csv.DictReader(lines))

    assert row["ClassyFy Status"] == "Completed"
    assert row["Kingdom"] == "Original kingdom"


@pytest.mark.django_db(transaction=True)
def test_name_only_live_label_remains_visible_without_chemont_fk():
    from apps.chemont.models import ChemOntNode
    from apps.classifications.models import ClassyfireLive
    from apps.classifications.result_writer import write_live_result
    from apps.classifications.services import snapshot_current_classification
    from apps.core.enums import SourceType
    from apps.remote.provider import ChemOntSlot, NormalizedClassification

    ChemOntNode.objects.create(
        numeric_id=1200,
        chemont_id="CHEMONT:0001200",
        name="Upstream name-only kingdom",
        ontology_version="2.1",
    )
    norm = NormalizedClassification(
        inchikey=ASPIRIN_INCHIKEY,
        kingdom=ChemOntSlot(chemont_id=None, name="Upstream name-only kingdom"),
        superclass=None,
        class_=None,
        subclass=None,
        direct_parent=None,
        raw_json={"inchikey": ASPIRIN_INCHIKEY},
    )

    outcome = write_live_result(
        ASPIRIN_INCHIKEY,
        norm,
        source_type=SourceType.WISHART_LIVE_ENTITY,
    )

    assert outcome.applied is True
    live = ClassyfireLive.objects.get(inchikey=ASPIRIN_INCHIKEY)
    assert live.kingdom_id is None

    snapshot = snapshot_current_classification(ASPIRIN_INCHIKEY)
    assert snapshot is not None
    assert snapshot["kingdom"] == {
        "chemont_id": None,
        "name": "Upstream name-only kingdom",
    }

    job, secret = _create_simple_job()
    item = job.items.get()
    assert item.classification_snapshot is not None
    assert item.classification_snapshot["kingdom"]["name"] == (
        "Upstream name-only kingdom"
    )

    client = Client()
    raw = _get_export_bytes(client, str(job.pk), secret, "csv")
    text = raw.decode("utf-8")
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    row = next(csv.DictReader(lines))
    assert row["Kingdom"] == "Upstream name-only kingdom"


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
    assert "InChIKey" in fieldnames
    assert "ClassyFy Status" in fieldnames


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
    assert data_rows[0]["InChIKey"] == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
    assert data_rows[1]["InChIKey"] == "RYYVLZVUVIJVGH-UHFFFAOYSA-N"


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
# 8. Requested status field present
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_requested_status_field_present():
    job, secret = _create_simple_job()
    client = Client()
    raw = _get_export_bytes(client, str(job.pk), secret, "csv")
    text_out = raw.decode("utf-8")
    lines = [line for line in text_out.splitlines() if not line.startswith("#")]
    reader = csv.DictReader(lines)
    fieldnames = reader.fieldnames or []
    assert "ClassyFy Status" in fieldnames


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
    dangerous = _make_chemont(900, "=Formula Kingdom")
    _make_live(ASPIRIN_INCHIKEY, kingdom_id=dangerous.numeric_id)
    rows = [{"raw": ASPIRIN_SMILES, "row_index": 0}]
    job, secret = create_job(rows, remote_enabled=False)
    client = Client()
    raw = _get_export_bytes(client, str(job.pk), secret, "csv")
    text = raw.decode("utf-8")
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    reader = csv.DictReader(lines)
    data_rows = list(reader)
    assert len(data_rows) == 1
    kingdom = data_rows[0]["Kingdom"]
    assert kingdom == "'=Formula Kingdom", (
        f"Expected injection-escaped value, got {kingdom!r}"
    )


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
    assert "InChIKey" in content


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


@pytest.mark.django_db(transaction=True)
def test_export_reuse_invalidates_after_job_items_resolve():
    from apps.batches.models import BatchItem
    from apps.batches.services import create_job_records, recompute_job_counters
    from apps.classifications.services import snapshot_current_classification
    from apps.core.enums import BatchItemStatus

    kingdom = _make_chemont(1300, "Fresh completed kingdom")
    _make_live(ASPIRIN_INCHIKEY, kingdom_id=kingdom.numeric_id)
    rows = list(parse_text_input(ASPIRIN_SMILES))
    job, secret = create_job_records(rows, remote_enabled=False)
    item = job.items.get()
    item.inchikey = ASPIRIN_INCHIKEY
    item.status = BatchItemStatus.PENDING_CLASSIFICATION
    item.save(update_fields=["inchikey", "status"])
    recompute_job_counters(job.pk)

    client = Client()
    stale_token = _create_export_token(client, str(job.pk), secret, "csv")
    stale_text = _download_export_bytes(client, stale_token).decode("utf-8")
    stale_rows = list(
        csv.DictReader(
            [line for line in stale_text.splitlines() if not line.startswith("#")]
        )
    )
    assert stale_rows[0]["ClassyFy Status"] == "Failed"

    BatchItem.objects.filter(pk=item.pk).update(
        status=BatchItemStatus.FOUND_LIVE_CACHE,
        classification_snapshot=snapshot_current_classification(ASPIRIN_INCHIKEY),
    )
    recompute_job_counters(job.pk)

    fresh_token = _create_export_token(client, str(job.pk), secret, "csv")
    fresh_text = _download_export_bytes(client, fresh_token).decode("utf-8")
    fresh_rows = list(
        csv.DictReader(
            [line for line in fresh_text.splitlines() if not line.startswith("#")]
        )
    )

    assert fresh_token != stale_token
    assert fresh_rows[0]["ClassyFy Status"] == "Completed"
    assert fresh_rows[0]["Kingdom"] == "Fresh completed kingdom"
