"""Tests for Task 1 (Gate 6): Remove query-string job secrets from API.

Verifies:
- GET /api/jobs/{id}?secret=... returns HTTP 400 with a message pointing to
  the X-Job-Secret header.
- GET /api/jobs/{id} with X-Job-Secret header returns HTTP 200.
- SecretRedactionFilter redacts ?secret= values and /api/exports/<token> paths
  in log records.
"""
from __future__ import annotations

import logging

import pytest

from apps.batches.ingest import parse_text_input
from apps.batches.services import create_job_records

# ---------------------------------------------------------------------------
# Local fixture — creates a BatchJob with a known plaintext secret.
# Mirrors the pattern used in test_security.py / test_batch_ingest.py.
# ---------------------------------------------------------------------------

ASPIRIN_SMILES = "CC(=O)Oc1ccccc1C(=O)O"


@pytest.fixture
def batch_job_with_secret(db):
    """Return (BatchJob, plaintext_secret) ready for API tests."""
    rows = list(parse_text_input(ASPIRIN_SMILES))
    job, plaintext_secret = create_job_records(rows, remote_enabled=False)
    return job, plaintext_secret


# ---------------------------------------------------------------------------
# Transport tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_api_rejects_query_string_job_secret(client, batch_job_with_secret):
    job, secret = batch_job_with_secret
    response = client.get(
        f"/api/jobs/{job.pk}?secret={secret}",
        HTTP_AUTHORIZATION="Bearer dev-token",
    )
    assert response.status_code == 400
    assert "X-Job-Secret" in response.json()["error"]


@pytest.mark.django_db
def test_api_accepts_header_job_secret(client, batch_job_with_secret):
    job, secret = batch_job_with_secret
    response = client.get(
        f"/api/jobs/{job.pk}",
        HTTP_X_JOB_SECRET=secret,
        HTTP_AUTHORIZATION="Bearer dev-token",
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Log redaction filter test
# ---------------------------------------------------------------------------


def test_sensitive_log_filter_redacts_query_and_token(caplog):
    from apps.core.security import SecretRedactionFilter

    record = logging.LogRecord(
        name="django.request",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        # The export token deliberately contains 's' and is followed by a
        # trailing " HTTP/1.1" — a regex that excludes the literal 's' (or that
        # eats whitespace) would leak part of a real secrets.token_urlsafe value.
        msg="GET /api/jobs/abc?secret=plain-secret /api/exports/s3cret-token-Xs HTTP/1.1",
        args=(),
        exc_info=None,
    )
    SecretRedactionFilter().filter(record)
    rendered = record.getMessage()
    assert "plain-secret" not in rendered
    assert "s3cret-token-Xs" not in rendered
    assert "[REDACTED]" in rendered
    # The redaction must stop at the path boundary, not consume the HTTP version.
    assert "HTTP/1.1" in rendered
