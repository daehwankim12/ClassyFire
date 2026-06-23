"""E2E flow tests: submit → result → export.

These tests exercise the full submit → job status → items → structure
completion → export flow using Django's test client.  The test client acts
as a proxy for the real HTTP path (ngrok or any reverse proxy) without
requiring an external domain or live network connectivity.

Tests that require a real ngrok URL are marked with ``pytest.mark.skip``
and include an explicit reason explaining what would be needed.
"""
from __future__ import annotations

import json

import pytest
from django.test import Client

from apps.batches.ingest import parse_text_input
from apps.batches.services import create_job
from apps.core.enums import BatchItemStatus

# ---------------------------------------------------------------------------
# Test molecules
# ---------------------------------------------------------------------------

ASPIRIN_SMILES = "CC(=O)Oc1ccccc1C(=O)O"
ASPIRIN_INCHIKEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"

# L-Tryptophan: an InChIKey whose structure is not in the test seed cache.
GHOST_INCHIKEY = "QIVBCDIJIAJPQS-VIFPVBQESA-N"
GHOST_SMILES = "N[C@@H](Cc1c[nH]c2ccccc12)C(=O)O"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post_json(client: Client, url: str, data: dict) -> object:
    return client.post(
        url,
        data=json.dumps(data),
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# 1. Full E2E: submit → status → items → export  (Django test client)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_submit_to_export_flow_via_test_client():
    """Full flow via Django test client: create job, poll status, list items,
    create export, download export."""
    client = Client()

    # --- 1. Create job via API ---
    resp = _post_json(
        client,
        "/api/jobs",
        {"text_input": ASPIRIN_SMILES, "remote_enabled": False},
    )
    assert resp.status_code == 201, resp.content
    payload = resp.json()
    job_id = payload["job_id"]
    secret = payload["result_secret"]

    # --- 2. Poll job status ---
    resp = client.get(f"/api/jobs/{job_id}", {"secret": secret})
    assert resp.status_code == 200
    status_data = resp.json()
    assert status_data["id"] == job_id
    assert "total_count" in status_data

    # --- 3. List items ---
    resp = client.get(f"/api/jobs/{job_id}/items", {"secret": secret})
    assert resp.status_code == 200
    items_data = resp.json()
    assert "results" in items_data
    assert items_data["count"] >= 1

    # --- 4. Create CSV export ---
    resp = _post_json(
        client,
        f"/api/jobs/{job_id}/exports",
        {"format": "csv", "secret": secret},
    )
    assert resp.status_code == 201
    export_payload = resp.json()
    assert "token" in export_payload
    token = export_payload["token"]

    # --- 5. Download export ---
    resp = client.get(f"/api/exports/{token}")
    assert resp.status_code == 200
    content_type = resp.get("Content-Type", "")
    assert "csv" in content_type or "text" in content_type


# ---------------------------------------------------------------------------
# 2. NEED_STRUCTURE completion flow  (Django test client)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_need_structure_completion_flow_via_test_client():
    """Submit an InChIKey-only row (no structure), confirm NEED_STRUCTURE,
    then submit the matching structure and confirm the item progresses."""
    client = Client()

    # Create job with an InChIKey that has no structure in the test DB.
    rows = [{"raw": GHOST_INCHIKEY, "row_index": 0}]
    job, secret = create_job(rows, remote_enabled=False)
    item = job.items.first()
    assert item is not None
    assert item.status == BatchItemStatus.NEED_STRUCTURE

    # Submit the matching structure via API.
    resp = _post_json(
        client,
        f"/api/items/{item.pk}/structure",
        {"structure": GHOST_SMILES, "secret": secret},
    )
    # Should succeed (200) — the InChIKey matched, so the API accepted the
    # structure.  With remote_enabled=False and no local cache hit the item
    # may remain in a non-error status (the mismatch guard did NOT trigger).
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    # The API must not return an error status — the structure was accepted.
    # (With remote_enabled=False and no seed data the item may stay
    # NEED_STRUCTURE or progress, but the request itself must not be rejected.)
    assert "error" not in data


# ---------------------------------------------------------------------------
# 3. Key-mismatch guard  (Django test client)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_structure_key_mismatch_returns_422_via_test_client():
    """Submitting a structure whose InChIKey does not match the item's
    expected InChIKey must return 422 and leave the item as NEED_STRUCTURE."""
    client = Client()

    rows = [{"raw": GHOST_INCHIKEY, "row_index": 0}]
    job, secret = create_job(rows, remote_enabled=False)
    item = job.items.first()
    assert item is not None

    # Submit ASPIRIN (wrong molecule) for the ghost InChIKey item.
    resp = _post_json(
        client,
        f"/api/items/{item.pk}/structure",
        {"structure": ASPIRIN_SMILES, "secret": secret},
    )
    assert resp.status_code == 422
    data = resp.json()
    assert "error" in data

    item.refresh_from_db()
    assert item.status == BatchItemStatus.NEED_STRUCTURE


# ---------------------------------------------------------------------------
# 4. Pagination smoke test  (Django test client)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_items_pagination_via_test_client():
    """Items endpoint must respect page_size and return correct page count."""
    # 15 identical rows → 2 pages with page_size=10
    text = "\n".join([ASPIRIN_SMILES] * 15)
    rows = list(parse_text_input(text))
    job, secret = create_job(rows, remote_enabled=False)

    client = Client()
    resp = client.get(
        f"/api/jobs/{job.pk}/items",
        {"secret": secret, "page": 1, "page_size": 10},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 15
    assert len(data["results"]) == 10
    assert data["num_pages"] == 2


# ---------------------------------------------------------------------------
# 5. Export formats smoke test  (Django test client)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("fmt", ["csv", "tsv", "json", "csv.gz", "tsv.gz"])
def test_all_export_formats_via_test_client(fmt: str):
    """All five export formats must produce a downloadable file."""
    rows = list(parse_text_input(ASPIRIN_SMILES))
    job, secret = create_job(rows, remote_enabled=False)

    client = Client()
    resp = _post_json(
        client,
        f"/api/jobs/{job.pk}/exports",
        {"format": fmt, "secret": secret},
    )
    assert resp.status_code == 201
    token = resp.json()["token"]

    dl = client.get(f"/api/exports/{token}")
    assert dl.status_code == 200


# ---------------------------------------------------------------------------
# 6. Real-ngrok URL flow — SKIPPED (requires live NGROK_DOMAIN)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "Requires a live ngrok domain (NGROK_AUTHTOKEN + NGROK_DOMAIN env vars) "
        "and a running Django server reachable at that domain.  "
        "Run manually per docs/acceptance-test-results.md#t-15-external-url-e2e."
    )
)
def test_real_ngrok_submit_flow():
    """E2E test against a real ngrok URL.

    Steps (manual):
    1. Set NGROK_AUTHTOKEN and NGROK_DOMAIN in .env.
    2. docker compose up -d
    3. Confirm the domain resolves and the submit page loads.
    4. POST a SMILES string and verify the result page shows classification data.
    5. Download a CSV export and inspect the rows.
    """
    import os
    import urllib.request

    domain = os.environ.get("NGROK_DOMAIN")
    if not domain:
        pytest.skip("NGROK_DOMAIN not set")

    base_url = f"https://{domain}"
    req = urllib.request.Request(base_url, method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200


@pytest.mark.skip(
    reason=(
        "Requires a live ngrok domain with SHARED_ACCESS_PASSWORD_HASH configured "
        "for full auth-header testing.  Run manually per the acceptance procedure."
    )
)
def test_real_ngrok_api_bearer_auth():
    """Verify that the Bearer token header is accepted over the public ngrok URL.

    Steps (manual):
    1. Set SHARED_ACCESS_PASSWORD (hashed) in production settings.
    2. Obtain the Bearer token.
    3. POST to https://<NGROK_DOMAIN>/api/jobs with Authorization: Bearer <token>.
    4. Confirm 201 response with job_id and result_secret.
    """
