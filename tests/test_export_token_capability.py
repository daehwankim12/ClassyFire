"""Tests for Gate 7 Task 1: capability-token export download + no query-secret auth.

Security model under test
-------------------------
- The export-download URL carries a 256-bit ``secrets.token_urlsafe(32)`` token.
  A valid, non-expired token IS the authorization: a browser GET to
  ``/api/exports/<token>`` with NO ``?secret=``, NO header, NO cookie must serve
  the file even in SECURE mode (SHARED_ACCESS_PASSWORD_HASH set).
- An unknown or expired token returns HTTP 404 (NOT 401/403) so the response
  never reveals the auth model or whether a token exists.
- No job-scoped route accepts a ``?secret=`` query parameter anymore; the secret
  must travel in the ``X-Job-Secret`` header (or JSON body for POST routes).
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from django.test import Client, override_settings

from apps.batches.ingest import parse_text_input
from apps.batches.services import create_job

ASPIRIN_SMILES = "CC(=O)Oc1ccccc1C(=O)O"

# A valid pbkdf2 hash of "test-shared-secret-12345" — mirrors test_security.py so
# SECURE mode (a configured shared-access password) can be exercised.
_TEST_PASSWORD = "test-shared-secret-12345"
_TEST_HASH = "pbkdf2_sha256$1000000$testsalt0001$cjObEZ7NhyvoAp9c0xgafKMXqcwEbwyIvu/yNHCwLig="


def _create_simple_job():
    """Create a one-row ASPIRIN job; return (job, plaintext_secret)."""
    rows = list(parse_text_input(ASPIRIN_SMILES))
    return create_job(rows, remote_enabled=False)


def _create_export_token(client: Client, job, secret: str, fmt: str = "csv") -> str:
    """Create an export via the authenticated API and return its token."""
    resp = client.post(
        f"/api/jobs/{job.pk}/exports",
        data=json.dumps({"format": fmt, "secret": secret}),
        content_type="application/json",
    )
    assert resp.status_code == 201, f"Export creation failed: {resp.status_code} {resp.content!r}"
    return str(resp.json()["token"])


def _download_body(resp) -> bytes:
    if hasattr(resp, "streaming_content"):
        return b"".join(resp.streaming_content)
    return resp.content


# ---------------------------------------------------------------------------
# 1. Valid token alone authorizes the download — no secret, no header, SECURE mode.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
@override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
def test_download_with_valid_token_no_secret_succeeds() -> None:
    job, secret = _create_simple_job()
    # Create the export with proper job-secret authorization (header form).
    create_client = Client()
    token = create_client.post(
        f"/api/jobs/{job.pk}/exports",
        data=json.dumps({"format": "csv"}),
        content_type="application/json",
        HTTP_X_JOB_SECRET=secret,
        HTTP_AUTHORIZATION=f"Bearer {_TEST_PASSWORD}",
    ).json()["token"]

    # A FRESH client (no session, no cookies) downloads via a plain token URL with
    # NO secret, NO X-Job-Secret, NO Bearer — the token is the capability.
    download_client = Client()
    resp = download_client.get(f"/api/exports/{token}")

    assert resp.status_code == 200, (
        f"Valid token must download with no secret/header, got {resp.status_code}"
    )
    body = _download_body(resp)
    assert body, "Download returned an empty body"
    assert b"InChIKey" in body, "CSV export body should contain the header row"


# ---------------------------------------------------------------------------
# 2. Unknown token → 404 (no existence leak, no auth-model leak).
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
@override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
def test_download_unknown_token_404() -> None:
    client = Client()
    resp = client.get("/api/exports/this-token-does-not-exist-xyz")
    assert resp.status_code == 404, (
        f"Unknown token must be 404 (not 401/403, which would leak the auth model), "
        f"got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# 3. Expired token → 404.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
@override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
def test_download_expired_token_404() -> None:
    from apps.batches.models import ExportArtifact

    job, secret = _create_simple_job()
    create_client = Client()
    token = create_client.post(
        f"/api/jobs/{job.pk}/exports",
        data=json.dumps({"format": "csv"}),
        content_type="application/json",
        HTTP_X_JOB_SECRET=secret,
        HTTP_AUTHORIZATION=f"Bearer {_TEST_PASSWORD}",
    ).json()["token"]

    # Back-date expiry so get_export_path() returns None.
    past = dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=1)
    ExportArtifact.objects.filter(token=token).update(expires_at=past)

    resp = Client().get(f"/api/exports/{token}")
    assert resp.status_code == 404, (
        f"Expired token must be 404, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# 4. A job-scoped route no longer authenticates via ?secret=; the header does.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
@override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
def test_query_secret_no_longer_authenticates_shared_access() -> None:
    job, secret = _create_simple_job()

    # ONLY ?secret= in the query string, no header/bearer/cookie. The
    # query fallback is gone, and api._job_secret rejects query secrets — so the
    # shared-access gate denies the request.
    query_resp = Client().get(f"/api/jobs/{job.pk}?secret={secret}")
    assert query_resp.status_code in (400, 401, 403), (
        f"Query-string secret must NOT authenticate a job-scoped route, "
        f"got {query_resp.status_code}"
    )

    # The same secret via the X-Job-Secret header authorizes the job-scoped
    # response only after a bearer clears the shared-access gate in SECURE mode.
    header_resp = Client().get(
        f"/api/jobs/{job.pk}",
        HTTP_X_JOB_SECRET=secret,
        HTTP_AUTHORIZATION=f"Bearer {_TEST_PASSWORD}",
    )
    assert header_resp.status_code == 200, (
        f"X-Job-Secret header must authenticate, got {header_resp.status_code}"
    )
