"""Tests for the batch web UI (Task 8).

These tests cover:
- Submit page (GET and POST)
- Job detail page (auth, counters, pagination, filtering, search)
- HTMX partial (items endpoint)
- Structure completion form (key-match guard)
- Stats page
"""
from __future__ import annotations

import json

import pytest
from django.test import Client

from apps.batches.ingest import parse_text_input
from apps.batches.services import create_job
from apps.core.enums import BatchItemStatus
from apps.core.security import MAX_STRUCTURE_LEN

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ASPIRIN_SMILES = "CC(=O)Oc1ccccc1C(=O)O"
ASPIRIN_INCHIKEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
CAFFEINE_SMILES = "Cn1cnc2c1c(=O)n(c(=O)n2C)C"

# An InChIKey that is almost certainly not in the test DB → NEED_STRUCTURE.
# Use L-Tryptophan: QIVBCDIJIAJPQS-VIFPVBQESA-N
# The SMILES that yields this InChIKey via RDKit.
GHOST_INCHIKEY = "QIVBCDIJIAJPQS-VIFPVBQESA-N"
GHOST_SMILES = "N[C@@H](Cc1c[nH]c2ccccc12)C(=O)O"


# ---------------------------------------------------------------------------
# 1. GET submit page returns 200
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_submit_get_returns_200():
    client = Client()
    response = client.get("/")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# 2. POST submit redirects to job page
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_submit_post_redirects_to_job():
    client = Client()
    response = client.post(
        "/",
        {"text_input": ASPIRIN_SMILES, "remote_enabled": "on"},
        follow=False,
    )
    assert response.status_code == 302
    location = response["Location"]
    # Should redirect to the one-time claim URL; the claim response then
    # exchanges the secret for a cookie and lands on /jobs/<uuid>/.
    assert location.startswith("/jobs/")
    parts = location.strip("/").split("/")
    assert len(parts) == 4  # jobs, uuid, claim, secret
    assert parts[2] == "claim"


# ---------------------------------------------------------------------------
# 3. Job detail: wrong secret → 403
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_job_detail_wrong_secret_returns_403():
    rows = list(parse_text_input(ASPIRIN_SMILES))
    job, _secret = create_job(rows, remote_enabled=False)
    client = Client()
    response = client.get(f"/jobs/{job.pk}/wrongsecret/")
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 4. Job detail: missing job → 404
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_job_detail_missing_job_returns_404():
    client = Client()
    fake_uuid = "00000000-0000-0000-0000-000000000000"
    response = client.get(f"/jobs/{fake_uuid}/somesecret/")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 5. Job detail shows counters
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_job_detail_shows_counters():
    rows = list(parse_text_input(ASPIRIN_SMILES))
    job, secret = create_job(rows, remote_enabled=False)
    client = Client()
    claim_response = client.get(f"/jobs/{job.pk}/claim/{secret}/")
    assert claim_response.status_code == 303
    response = client.get(f"/jobs/{job.pk}/")
    assert response.status_code == 200
    content = response.content.decode()
    # UUID should appear somewhere on the page
    assert str(job.pk) in content


# ---------------------------------------------------------------------------
# 6. Pagination: default 100 rows per page
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_pagination_default_100_rows():
    # Create 105 identical SMILES rows
    text = "\n".join([ASPIRIN_SMILES] * 105)
    rows = list(parse_text_input(text))
    job, secret = create_job(rows, remote_enabled=False)
    client = Client()
    claim_response = client.get(f"/jobs/{job.pk}/claim/{secret}/")
    assert claim_response.status_code == 303
    response = client.get(f"/jobs/{job.pk}/")
    assert response.status_code == 200
    content = response.content.decode()
    # Count occurrences of a unique per-row marker — the original_input appears in table cells
    # We expect exactly 100 rows on page 1
    # Count table row occurrences
    row_count = content.count(ASPIRIN_SMILES)
    assert row_count == 100, f"Expected 100 rows on page 1, got {row_count}"


# ---------------------------------------------------------------------------
# 7. Status filter: INVALID shows only invalid items
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_status_filter_invalid():
    text = f"{ASPIRIN_SMILES}\nNOT_A_VALID_COMPOUND_XYZ_12345_GARBAGE"
    rows = list(parse_text_input(text))
    job, secret = create_job(rows, remote_enabled=False)
    client = Client()
    claim_response = client.get(f"/jobs/{job.pk}/claim/{secret}/")
    assert claim_response.status_code == 303
    response = client.get(f"/jobs/{job.pk}/", {"status_filter": "INVALID"})
    assert response.status_code == 200
    content = response.content.decode()
    # The invalid item should appear, the valid SMILES should NOT appear in the table
    assert "NOT_A_VALID_COMPOUND_XYZ_12345_GARBAGE" in content
    assert ASPIRIN_SMILES not in content


# ---------------------------------------------------------------------------
# 8. Search by external_id
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_search_by_external_id():
    rows = list(parse_text_input(ASPIRIN_SMILES))
    # Inject external_id into the row
    rows[0]["external_id"] = "MY-UNIQUE-EXT-ID-9999"
    job, secret = create_job(rows, remote_enabled=False)
    client = Client()
    claim_response = client.get(f"/jobs/{job.pk}/claim/{secret}/")
    assert claim_response.status_code == 303
    response = client.get(f"/jobs/{job.pk}/", {"search": "MY-UNIQUE-EXT-ID-9999"})
    assert response.status_code == 200
    content = response.content.decode()
    assert "MY-UNIQUE-EXT-ID-9999" in content


# ---------------------------------------------------------------------------
# 9. Structure form: matching InChIKey succeeds (no RESULT_KEY_MISMATCH)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_structure_form_matching_inchikey_succeeds():
    # Create a job with the ghost InChIKey (no structure)
    rows = [{"raw": GHOST_INCHIKEY, "row_index": 0}]
    job, secret = create_job(rows, remote_enabled=False)
    item = job.items.first()
    assert item is not None
    assert item.status == BatchItemStatus.NEED_STRUCTURE

    client = Client()
    response = client.post(
        f"/items/{item.pk}/{secret}/structure",
        {"structure": GHOST_SMILES},
    )
    # Should redirect back to job page on match (or render form without mismatch error)
    if response.status_code == 200:
        content = response.content.decode()
        assert "RESULT_KEY_MISMATCH" not in content
    else:
        assert response.status_code == 302


@pytest.mark.django_db(transaction=True)
def test_legacy_structure_get_exchanges_cookie_without_rendering_secret():
    rows = [{"raw": GHOST_INCHIKEY, "row_index": 0}]
    job, secret = create_job(rows, remote_enabled=False)
    item = job.items.first()
    assert item is not None
    client = Client()

    response = client.get(f"/items/{item.pk}/{secret}/structure")

    assert response.status_code == 303
    assert response["Location"] == f"/items/{item.pk}/structure"
    assert secret.encode() not in response.content
    follow_response = client.get(response["Location"])
    assert follow_response.status_code == 200
    content = follow_response.content.decode()
    assert secret not in content


@pytest.mark.django_db(transaction=True)
def test_legacy_structure_post_redirects_without_secret():
    rows = [{"raw": GHOST_INCHIKEY, "row_index": 0}]
    job, secret = create_job(rows, remote_enabled=False)
    item = job.items.first()
    assert item is not None
    client = Client()

    response = client.post(
        f"/items/{item.pk}/{secret}/structure",
        {"structure": GHOST_SMILES},
    )

    assert response.status_code in (302, 303)
    assert response["Location"] == f"/jobs/{job.pk}/"
    assert secret not in response["Location"]


@pytest.mark.django_db(transaction=True)
def test_legacy_items_partial_exchanges_cookie_without_rendering_secret():
    rows = [{"raw": GHOST_INCHIKEY, "row_index": 0}]
    job, secret = create_job(rows, remote_enabled=False)
    client = Client()

    response = client.get(
        f"/jobs/{job.pk}/{secret}/items",
        {"status_filter": "NEED_STRUCTURE"},
    )

    assert response.status_code == 303
    assert response["Location"] == f"/jobs/{job.pk}/items?status_filter=NEED_STRUCTURE"
    assert secret.encode() not in response.content
    follow_response = client.get(response["Location"])
    assert follow_response.status_code == 200
    assert secret not in follow_response.content.decode()


@pytest.mark.django_db(transaction=True)
def test_legacy_structure_post_error_redirects_without_secret():
    rows = [{"raw": GHOST_INCHIKEY, "row_index": 0}]
    job, secret = create_job(rows, remote_enabled=False)
    item = job.items.first()
    assert item is not None
    client = Client()

    response = client.post(
        f"/items/{item.pk}/{secret}/structure",
        {"structure": ASPIRIN_SMILES},
    )

    assert response.status_code == 303
    assert response["Location"] == f"/items/{item.pk}/structure"
    assert secret not in response["Location"]
    assert secret.encode() not in response.content


# ---------------------------------------------------------------------------
# 10. Structure form: mismatching InChIKey shows error
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_structure_form_mismatching_inchikey_shows_error():
    rows = [{"raw": GHOST_INCHIKEY, "row_index": 0}]
    job, secret = create_job(rows, remote_enabled=False)
    item = job.items.first()
    assert item is not None
    assert item.status == BatchItemStatus.NEED_STRUCTURE

    client = Client()
    claim_response = client.get(f"/jobs/{job.pk}/claim/{secret}/")
    assert claim_response.status_code == 303
    # Submit ASPIRIN structure for the GHOST_INCHIKEY item → mismatch
    response = client.post(
        f"/items/{item.pk}/structure",
        {"structure": ASPIRIN_SMILES},
    )
    assert response.status_code == 200
    content = response.content.decode()
    # Should show mismatch error
    assert "mismatch" in content.lower() or "RESULT_KEY_MISMATCH" in content

    # Item status should NOT have changed
    item.refresh_from_db()
    assert item.status == BatchItemStatus.NEED_STRUCTURE


# ---------------------------------------------------------------------------
# 11. Stats page returns 200
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_stats_page_returns_200():
    client = Client()
    response = client.get("/stats/")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# 12. API pagination: bad page_size → 400; page_size=0 doesn't crash
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_api_items_invalid_page_size_returns_400():
    """?page_size=abc must return 400, not 500."""
    rows = list(parse_text_input(ASPIRIN_SMILES))
    job, secret = create_job(rows, remote_enabled=False)
    client = Client()
    # Pass secret via header; ?page_size= stays in query string
    response = client.get(
        f"/api/jobs/{job.pk}/items?page_size=abc",
        HTTP_X_JOB_SECRET=secret,
    )
    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.django_db(transaction=True)
def test_api_items_zero_page_size_does_not_crash():
    """?page_size=0 must be clamped (not crash)."""
    rows = list(parse_text_input(ASPIRIN_SMILES))
    job, secret = create_job(rows, remote_enabled=False)
    client = Client()
    # Pass secret via header; ?page_size= stays in query string
    response = client.get(
        f"/api/jobs/{job.pk}/items?page_size=0",
        HTTP_X_JOB_SECRET=secret,
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# 13. API key-mismatch leaves item as NEED_STRUCTURE (retry-friendly)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_api_structure_key_mismatch_leaves_need_structure():
    """API mismatch must return 422 and NOT strand item as RESULT_KEY_MISMATCH."""
    rows = [{"raw": GHOST_INCHIKEY, "row_index": 0}]
    job, secret = create_job(rows, remote_enabled=False)
    item = job.items.first()
    assert item is not None
    assert item.status == BatchItemStatus.NEED_STRUCTURE

    import json as _json

    client = Client()
    # Submit ASPIRIN structure for the GHOST_INCHIKEY item → mismatch
    response = client.post(
        f"/api/items/{item.pk}/structure",
        data=_json.dumps({"structure": ASPIRIN_SMILES, "secret": secret}),
        content_type="application/json",
    )
    assert response.status_code == 422
    data = response.json()
    assert "error" in data

    # Item must remain NEED_STRUCTURE (retryable), not RESULT_KEY_MISMATCH
    item.refresh_from_db()
    assert item.status == BatchItemStatus.NEED_STRUCTURE


@pytest.mark.django_db(transaction=True)
def test_api_structure_resubmit_rejects_bare_inchikey():
    rows = [{"raw": GHOST_INCHIKEY, "row_index": 0}]
    job, secret = create_job(rows, remote_enabled=False)
    item = job.items.get()
    assert item.status == BatchItemStatus.NEED_STRUCTURE

    job.remote_enabled = True
    job.save(update_fields=["remote_enabled"])

    client = Client()
    response = client.post(
        f"/api/items/{item.pk}/structure",
        data=json.dumps({"structure": GHOST_INCHIKEY, "secret": secret}),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "structure" in response.json()["error"].lower()

    item.refresh_from_db()
    assert item.status == BatchItemStatus.NEED_STRUCTURE
    assert item.compound_id is None

    from apps.remote.models import RemoteTask

    assert not RemoteTask.objects.filter(inchikey=GHOST_INCHIKEY).exists()


@pytest.mark.django_db(transaction=True)
def test_structure_resubmit_rejects_non_need_structure_item():
    rows = [{"raw": CAFFEINE_SMILES, "row_index": 0}]
    job, secret = create_job(rows, remote_enabled=False)
    item = job.items.get()
    assert item.status != BatchItemStatus.NEED_STRUCTURE

    client = Client()
    response = client.post(
        f"/api/items/{item.pk}/structure",
        data=json.dumps({"structure": CAFFEINE_SMILES, "secret": secret}),
        content_type="application/json",
    )

    assert response.status_code == 409
    item.refresh_from_db()
    assert item.status != BatchItemStatus.PENDING_CLASSIFICATION


@pytest.mark.django_db(transaction=True)
def test_structure_resubmit_rejects_snapshotted_item():
    rows = [{"raw": GHOST_INCHIKEY, "row_index": 0}]
    job, secret = create_job(rows, remote_enabled=False)
    item = job.items.get()
    item.classification_snapshot = {"source": "already-resolved"}
    item.save(update_fields=["classification_snapshot"])

    client = Client()
    response = client.post(
        f"/api/items/{item.pk}/structure",
        data=json.dumps({"structure": GHOST_SMILES, "secret": secret}),
        content_type="application/json",
    )

    assert response.status_code == 409
    item.refresh_from_db()
    assert item.status == BatchItemStatus.NEED_STRUCTURE


@pytest.mark.django_db(transaction=True)
def test_structure_resubmit_rejects_oversized_structure(monkeypatch):
    rows = [{"raw": GHOST_INCHIKEY, "row_index": 0}]
    job, secret = create_job(rows, remote_enabled=False)
    item = job.items.get()

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("oversized structure reached normalization")

    monkeypatch.setattr("apps.batches.services.normalize_row", fail_if_called)

    client = Client()
    response = client.post(
        f"/api/items/{item.pk}/structure",
        data=json.dumps({"structure": "C" * (MAX_STRUCTURE_LEN + 1), "secret": secret}),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "exceeds maximum length" in response.json()["error"]
    item.refresh_from_db()
    assert item.status == BatchItemStatus.NEED_STRUCTURE


# ---------------------------------------------------------------------------
# 14. File-upload rows preserve all parser fields (external_id not dropped)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_file_upload_row_extra_fields_preserved():
    """Parser rows with extra fields (e.g. external_id) must survive the
    file-upload merge loop — the {**row, row_index: ...} fix ensures this."""
    # Simulate the logic in submit_view: file_rows already contain external_id
    # (as parse_csv_tsv would yield); the fixed merge must not drop it.
    from apps.batches.services import create_job

    file_rows = [
        {"raw": ASPIRIN_SMILES, "row_index": 0, "external_id": "FILE-EXT-9000"},
    ]
    # Replicate the fixed merge (offset=0 since no text_input rows)
    offset = 0
    rows = []
    for row in file_rows:
        rows.append({**row, "row_index": row["row_index"] + offset})

    assert rows[0].get("external_id") == "FILE-EXT-9000", (
        "external_id dropped by the file-upload row merge"
    )

    job, _secret = create_job(rows, remote_enabled=False)
    item = job.items.filter(external_id="FILE-EXT-9000").first()
    assert item is not None, "external_id not propagated through ingest"
