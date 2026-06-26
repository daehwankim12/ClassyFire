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
    # Audit columns added in Gate-2 WS-E (F-12)
    "Canonical SMILES",
    "InChI",
    "Requested InChIKey",
    "Returned InChIKey",
    "Remote Query ID",
    "Classification Version",
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

    # Check the original 9 classification columns
    assert row["InChIKey"] == ASPIRIN_INCHIKEY
    assert row["ClassyFy Status"] == "Completed"
    assert row["Kingdom"] == "Organic compounds"
    assert row["Superclass"] == "Lipids and lipid-like molecules"
    assert row["Class"] == "Fatty Acyls"
    assert row["Subclass"] == "Fatty acid esters"
    assert row["Parent Level 1"] == "Acyl carnitines"
    assert row["Parent Level 2"] == ""
    assert row["Parent Level 3"] == ""
    # Audit columns are present (may be empty for this test setup)
    for col in ("Canonical SMILES", "InChI", "Requested InChIKey",
                "Returned InChIKey", "Remote Query ID", "Classification Version"):
        assert col in row, f"Audit column '{col}' missing from CSV"


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
def test_csv_export_has_no_citation_header():
    """Exports no longer carry an inline citation/comment header — attribution
    for the CC-BY seed dataset lives in NOTICE.md. The CSV must begin directly
    with the column header row, with no leading ``#`` comment lines."""
    job, secret = _create_simple_job()
    client = Client()
    text = _get_export_bytes(client, str(job.pk), secret, "csv").decode("utf-8")
    assert not text.startswith("#"), f"unexpected comment header: {text[:80]!r}"
    assert "Citation:" not in text
    assert "Seed dataset:" not in text


# ---------------------------------------------------------------------------
# 10. No citation metadata in JSON (no per-row _citation key)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_json_export_has_no_citation_key():
    """JSON rows no longer carry the per-row ``_citation`` attribution key."""
    job, secret = _create_simple_job()
    client = Client()
    raw = _get_export_bytes(client, str(job.pk), secret, "json")
    data = json.loads(raw.decode("utf-8"))
    assert len(data) >= 1
    assert "_citation" not in data[0]


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

    stale_download = client.get(f"/api/exports/{stale_token}")
    assert stale_download.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_legacy_export_metadata_without_rows_signature_is_not_served():
    """An artifact whose rows_signature no longer matches current job state is not served.

    The DB model (ExportArtifact) is now the source of truth.  We simulate a
    stale artifact by deleting the DB row so neither the DB record nor the
    sidecar (which also has rows_signature removed) can validate the token.
    """
    from apps.batches import exports
    from apps.batches.models import ExportArtifact

    job, secret = _create_simple_job()
    client = Client()
    token = _create_export_token(client, str(job.pk), secret, "csv")

    # Remove the DB artifact — primary source of truth gone
    ExportArtifact.objects.filter(token=token).delete()

    # Also corrupt the sidecar (legacy fallback) to remove rows_signature
    meta_path = exports._meta_path(token)
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        meta.pop("rows_signature", None)
        meta_path.write_text(json.dumps(meta))

    response = client.get(f"/api/exports/{token}")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Gate-2 WS-E (F-12) — NEW TESTS
# ---------------------------------------------------------------------------


# 14. Streaming: .iterator() is used and no full-list materialisation
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_streaming_uses_iterator(monkeypatch):
    """Export generation calls .iterator() with a chunk_size (memory-bounded)."""
    iterator_called = []
    real_iterator = __import__(
        "django.db.models.query", fromlist=["QuerySet"]
    ).QuerySet.iterator

    def spy_iterator(self, chunk_size=None):
        iterator_called.append(chunk_size)
        return real_iterator(self, chunk_size=chunk_size)

    monkeypatch.setattr(
        "django.db.models.query.QuerySet.iterator", spy_iterator
    )

    job, secret = _create_simple_job()
    client = Client()
    _get_export_bytes(client, str(job.pk), secret, "csv")

    assert len(iterator_called) > 0, (
        "Expected .iterator() to be called during export generation"
    )
    # A real chunk_size must be passed (memory-bounding the fetch), not None.
    assert any(cs is not None and cs > 0 for cs in iterator_called), (
        f"Expected .iterator(chunk_size=N>0); got chunk sizes {iterator_called}"
    )


@pytest.mark.django_db(transaction=True)
def test_streaming_write_consumes_rows_lazily(monkeypatch):
    """_write_export_to_temp pulls rows one at a time — no full-list build.

    We feed it a generator that records the maximum number of rows that have
    been pulled but NOT yet consumed by the writer.  A streaming writer pulls
    one row, writes it, then pulls the next, so the in-flight count never
    exceeds 1.  A writer that materialised list(rows) would pull all N rows
    before writing any, driving the in-flight count to N.
    """
    from apps.batches import exports as exp_module

    job, _secret = _create_simple_job()

    n_rows = 50
    state = {"in_flight": 0, "max_in_flight": 0, "produced": 0}

    base_row = dict.fromkeys(EXPECTED_COLUMNS, "")

    def tracking_generator(_job):
        for i in range(n_rows):
            state["in_flight"] += 1
            state["produced"] += 1
            state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
            row = dict(base_row)
            row["InChIKey"] = f"ROW{i:026d}"[:27]
            yield row
            # Once the writer pulls the NEXT row, this one has been consumed.
            state["in_flight"] -= 1

    monkeypatch.setattr(exp_module, "_iter_rows_for_job", tracking_generator)

    tmp_path, _sig, row_count, _bytes = exp_module._write_export_to_temp(
        job, "csv", "lazytoken"
    )
    exp_module._discard_temp(tmp_path)

    assert state["produced"] == n_rows
    assert row_count == n_rows
    # Streaming: at most one row in flight at any instant.
    assert state["max_in_flight"] == 1, (
        f"Writer is not lazy: peak in-flight rows was {state['max_in_flight']} "
        f"(expected 1 for a streaming writer)"
    )


# 15. ExportArtifact created with checksum + expiry + row_count
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_export_artifact_created_with_checksum_expiry_row_count():
    """Creating an export persists an ExportArtifact with all required fields."""
    import datetime as dt
    import re

    from apps.batches.models import ExportArtifact

    job, secret = _create_simple_job()
    client = Client()
    resp = client.post(
        f"/api/jobs/{job.pk}/exports",
        data=json.dumps({"format": "csv", "secret": secret}),
        content_type="application/json",
    )
    assert resp.status_code == 201
    token = resp.json()["token"]

    artifact = ExportArtifact.objects.get(token=token)

    # checksum: 64-char lowercase hex
    assert re.fullmatch(r"[0-9a-f]{64}", artifact.checksum), (
        f"checksum is not 64-char hex: {artifact.checksum!r}"
    )
    # byte_size non-negative
    assert artifact.byte_size >= 0
    # row_count matches the job (1 row)
    assert artifact.row_count == 1
    # expires_at is in the future
    now = dt.datetime.now(tz=dt.UTC)
    assert artifact.expires_at > now
    # rows_signature: 64-char hex
    assert re.fullmatch(r"[0-9a-f]{64}", artifact.rows_signature)
    # path points to an existing file
    from pathlib import Path
    assert Path(artifact.path).exists()


# 16. ExportArtifact dedup returns same token
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_export_artifact_dedup_returns_same_token():
    """Two export requests for the same job+format return the same token via DB dedup."""
    from apps.batches.models import ExportArtifact

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
    assert token1 == token2, "Same job+format should return the same token (DB dedup)"

    # Only one artifact row should exist for this job+fmt
    count = ExportArtifact.objects.filter(job=job, fmt="csv").count()
    assert count == 1, f"Expected 1 artifact, got {count}"


# 17. Expired artifact pruned by cleanup
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_expired_artifact_pruned_by_cleanup():
    """Cleanup deletes expired ExportArtifact rows and their files."""
    import datetime as dt
    from pathlib import Path

    from apps.batches.models import ExportArtifact
    from apps.operations.cleanup import cleanup_expired

    job, secret = _create_simple_job()
    client = Client()
    resp = client.post(
        f"/api/jobs/{job.pk}/exports",
        data=json.dumps({"format": "csv", "secret": secret}),
        content_type="application/json",
    )
    assert resp.status_code == 201
    token = resp.json()["token"]

    artifact = ExportArtifact.objects.get(token=token)
    artifact_path = Path(artifact.path)
    assert artifact_path.exists()

    # Force expiry by back-dating expires_at
    past = dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=1)
    ExportArtifact.objects.filter(token=token).update(expires_at=past)

    # Also back-date the sidecar file mtime if present
    from apps.batches import exports as exp_module
    meta = exp_module._meta_path(token)
    if meta.exists():
        import json as _json
        raw = _json.loads(meta.read_text())
        raw["expires_at"] = past.isoformat()
        meta.write_text(_json.dumps(raw))

    result = cleanup_expired()
    assert result.exports_deleted > 0

    # DB row pruned
    assert not ExportArtifact.objects.filter(token=token).exists()
    # File pruned
    assert not artifact_path.exists()


# 18. Leading-whitespace injection escaping
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_leading_whitespace_injection_escaping():
    """_escape_cell escapes dangerous chars that follow leading whitespace."""
    from apps.batches.exports import _escape_cell

    # " =1+2" — space before formula
    assert _escape_cell(" =1+2") == "' =1+2", (
        "Leading-space formula should be escaped"
    )
    # "\t+cmd" — tab before +
    assert _escape_cell("\t+cmd") == "'\t+cmd", (
        "Leading-tab formula should be escaped"
    )
    # "@x" — no whitespace, direct dangerous char
    assert _escape_cell("@x") == "'@x", (
        "Direct @ should be escaped"
    )
    # Safe values
    assert _escape_cell("hello") == "hello"
    assert _escape_cell("  hello") == "  hello"
    assert _escape_cell(None) == ""


# 19. Audit columns present and correct
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_audit_columns_present_and_correct():
    """Export rows include audit columns from pinned provenance."""
    from apps.classifications.result_writer import write_live_result
    from apps.core.enums import SourceType
    from apps.remote.provider import ChemOntSlot, NormalizedClassification

    norm = NormalizedClassification(
        inchikey=ASPIRIN_INCHIKEY,
        kingdom=ChemOntSlot(chemont_id=None, name="Organic compounds"),
        superclass=None,
        class_=None,
        subclass=None,
        direct_parent=None,
        raw_json={"inchikey": ASPIRIN_INCHIKEY, "query_id": "remote-q-42"},
        classification_version="v99",
    )
    write_live_result(
        ASPIRIN_INCHIKEY,
        norm,
        source_type=SourceType.WISHART_LIVE_ENTITY,
        remote_query_id="remote-q-42",
    )

    job, secret = _create_simple_job()
    client = Client()
    raw = _get_export_bytes(client, str(job.pk), secret, "csv")
    text = raw.decode("utf-8")
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    row = next(csv.DictReader(lines))

    assert "Requested InChIKey" in row
    assert "Returned InChIKey" in row
    assert "Remote Query ID" in row
    assert "Classification Version" in row
    assert row["Requested InChIKey"] == ASPIRIN_INCHIKEY
    # Audit columns from classification event
    assert row["Remote Query ID"] == "remote-q-42"
    assert row["Classification Version"] == "v99"


# 20. Byte-stability after live supersede (WS-A invariant regression test)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_byte_stability_after_live_supersede():
    """Export bytes are unchanged after a live classification is superseded.

    Gate-2 WS-E must not regress the WS-A byte-stability guarantee: once an
    export is created it reads from the pinned resolved_classification event,
    never from the live table.
    """
    kingdom_v1 = _make_chemont(2000, "Kingdom V1")
    kingdom_v2 = _make_chemont(2001, "Kingdom V2")
    _make_live(ASPIRIN_INCHIKEY, kingdom_id=kingdom_v1.numeric_id, version="v1")

    job, secret = _create_simple_job()
    client = Client()

    # First export — captures V1 kingdom
    raw1 = _get_export_bytes(client, str(job.pk), secret, "csv")

    # Supersede live classification
    from apps.classifications.models import ClassyfireLive
    ClassyfireLive.objects.filter(inchikey=ASPIRIN_INCHIKEY).update(
        kingdom_id=kingdom_v2.numeric_id,
        classification_version="v2",
    )

    # Second export with same token (dedup) — must be byte-identical to first
    raw2 = _get_export_bytes(client, str(job.pk), secret, "csv")
    assert raw1 == raw2, (
        "Export bytes changed after live supersede — byte-stability invariant broken"
    )
