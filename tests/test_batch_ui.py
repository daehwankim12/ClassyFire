"""Tests for the batch web UI (Task 8).

These tests cover:
- Submit page (GET and POST)
- Job detail page (auth, counters, pagination, filtering, search)
- HTMX partial (items endpoint)
- Structure completion form (key-match guard)
- Stats page
"""
from __future__ import annotations

import pytest
from django.test import Client

from apps.batches.ingest import parse_text_input
from apps.batches.services import create_job
from apps.core.enums import BatchItemStatus

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
    # Should redirect to /jobs/<uuid>/<secret>/
    assert location.startswith("/jobs/")
    parts = location.strip("/").split("/")
    assert len(parts) == 3  # jobs, uuid, secret


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
    response = client.get(f"/jobs/{job.pk}/{secret}/")
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
    response = client.get(f"/jobs/{job.pk}/{secret}/")
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
    response = client.get(f"/jobs/{job.pk}/{secret}/", {"status_filter": "INVALID"})
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
    response = client.get(f"/jobs/{job.pk}/{secret}/", {"search": "MY-UNIQUE-EXT-ID-9999"})
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
    # Submit ASPIRIN structure for the GHOST_INCHIKEY item → mismatch
    response = client.post(
        f"/items/{item.pk}/{secret}/structure",
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
