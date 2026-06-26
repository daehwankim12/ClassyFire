"""Tests for Task 11: Security and Access Controls.

Covers:
- Unauthenticated API access -> 401/403 (HIGH finding: create_job_view)
- Authenticated API access via Bearer token -> succeeds
- CSRF policy: csrf_exempt endpoints require auth token (MEDIUM finding)
- Result URL secret validation: wrong/missing -> 403; correct -> 200
- prod settings: HSTS, secure cookies, SECURE_PROXY_SSL_HEADER
- Docker Compose port exposure: only proxy publishes host ports
- Upload safety: size limit, row length, decompression ratio, SDF property
- Secret redaction: logging helper never leaks secrets or structures
"""
from __future__ import annotations

import importlib
import io
import json
import logging
import os
import sys
import types
import zipfile
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml
from django.test import Client, override_settings

from apps.batches.ingest import parse_text_input
from apps.batches.services import create_job
from apps.core.security import (
    MAX_DECOMPRESSION_RATIO,
    MAX_SDF_PROPERTY_LEN,
    MAX_STRUCTURE_LEN,
    DecompressionBombError,
    RowTooLongError,
    SdfPropertyTooLargeError,
    SecretRedactionFilter,
    UploadTooLargeError,
    check_decompression_ratio,
    check_sdf_property_length,
    check_structure_length,
    check_upload_size,
    redact_for_log,
)


def test_secret_redaction_filter_redacts_claim_and_legacy_paths():
    """Claim-path and legacy job-path secrets are redacted, not just query secrets."""
    secret = "sentinel-invalid-claim-secret"
    job_id = "00000000-0000-0000-0000-000000000000"
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(SecretRedactionFilter())
    logger = logging.getLogger("test.secret.redaction")
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        logger.warning("Forbidden: /jobs/%s/claim/%s/", job_id, secret)
        logger.warning("Forbidden: /api/jobs/%s?secret=%s", job_id, secret)
        logger.warning("Forbidden: /jobs/%s/items/%s/", job_id, secret)
        logger.warning("Forbidden: /jobs/%s/structure/%s/", job_id, secret)
    finally:
        logger.removeHandler(handler)

    output = stream.getvalue()
    assert secret not in output
    assert quote(secret) not in output
    assert "[REDACTED]" in output

ASPIRIN_SMILES = "CC(=O)Oc1ccccc1C(=O)O"
GHOST_INCHIKEY = "QIVBCDIJIAJPQS-VIFPVBQESA-N"
GHOST_SMILES = "N[C@@H](Cc1c[nH]c2ccccc12)C(=O)O"

# Fixed test credentials.  Password hashed with pbkdf2_sha256 / salt=testsalt0001
# so no Django setup is required at import time and the value is stable.
# To regenerate: make_password("test-shared-secret-12345", salt="testsalt0001", hasher="pbkdf2_sha256")
_TEST_PASSWORD = "test-shared-secret-12345"
_TEST_HASH = "pbkdf2_sha256$1000000$testsalt0001$cjObEZ7NhyvoAp9c0xgafKMXqcwEbwyIvu/yNHCwLig="


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reload_prod_settings(
    monkeypatch: pytest.MonkeyPatch, env_overrides: dict[str, str]
) -> types.ModuleType:
    base_env = {
        "DJANGO_SECRET_KEY": "prod-secret-key-x" * 3,
        "POSTGRES_PASSWORD": "strongpassword",
        "NGROK_DOMAIN": "abc123.ngrok-free.app",
        "SHARED_ACCESS_PASSWORD_HASH": "pbkdf2:sha256:$hash",
        "POSTGRES_HOST": "db",
        "POSTGRES_DB": "classyfire",
        "POSTGRES_USER": "classyfire",
        "POSTGRES_PORT": "5432",
    }
    base_env.update(env_overrides)
    monkeypatch.setattr(os, "environ", {**os.environ, **base_env})
    reload_names = {
        "config.settings.base",
        "config.settings.prod",
        "apps.core.settings",
    }
    saved_modules = {
        mod_name: sys.modules[mod_name]
        for mod_name in reload_names
        if mod_name in sys.modules
    }
    for mod_name in reload_names:
        sys.modules.pop(mod_name, None)
    try:
        return importlib.import_module("config.settings.prod")
    finally:
        for mod_name in reload_names:
            sys.modules.pop(mod_name, None)
        sys.modules.update(saved_modules)


def _make_zip_bomb(compressed_size_target: int = 1024) -> bytes:
    """Create a zip whose decompressed content ratio is huge."""
    decompressed = b"\x00" * (int(MAX_DECOMPRESSION_RATIO * compressed_size_target) + 1)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("data.txt", decompressed)
    return buf.getvalue()


# ===========================================================================
# 1. Auth gate -- HIGH finding: create_job_view unauthenticated
# ===========================================================================


class TestAuthGateUnauthenticated:
    """[HIGH] POST /api/jobs must reject unauthenticated requests."""

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_post_api_jobs_no_auth_returns_401(self) -> None:
        client = Client()
        response = client.post(
            "/api/jobs",
            data=json.dumps({"text_input": ASPIRIN_SMILES}),
            content_type="application/json",
        )
        assert response.status_code in (401, 403), (
            f"Expected 401/403 without auth, got {response.status_code}"
        )

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_post_api_jobs_valid_bearer_returns_202(self) -> None:
        """Async ingest always returns 202 (staged, not yet processed)."""
        client = Client()
        response = client.post(
            "/api/jobs",
            data=json.dumps({"text_input": ASPIRIN_SMILES}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {_TEST_PASSWORD}",
        )
        assert response.status_code == 202, (
            f"Expected 202 with valid bearer, got {response.status_code}: {response.content}"
        )

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_post_api_jobs_wrong_bearer_returns_401(self) -> None:
        client = Client()
        response = client.post(
            "/api/jobs",
            data=json.dumps({"text_input": ASPIRIN_SMILES}),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer wrong-password",
        )
        assert response.status_code in (401, 403)

    @pytest.mark.django_db(transaction=True)
    @override_settings(
        SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH,
        JOB_CREATE_RATE_LIMIT_PER_MINUTE=1,
    )
    def test_job_create_rate_limit_rejects_abuse(self) -> None:
        client = Client()
        payload = {"text_input": ASPIRIN_SMILES, "remote_enabled": False}
        auth = {"HTTP_AUTHORIZATION": f"Bearer {_TEST_PASSWORD}"}

        first = client.post(
            "/api/jobs",
            data=json.dumps(payload),
            content_type="application/json",
            **auth,
        )
        second = client.post(
            "/api/jobs",
            data=json.dumps(payload),
            content_type="application/json",
            **auth,
        )

        assert first.status_code == 202
        assert second.status_code == 429

    @pytest.mark.django_db(transaction=True)
    @override_settings(
        SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH,
        DISK_UPLOAD_PAUSE_PERCENT=0,
    )
    def test_disk_high_water_blocks_job_create(self) -> None:
        client = Client()
        response = client.post(
            "/api/jobs",
            data=json.dumps({"text_input": ASPIRIN_SMILES, "remote_enabled": False}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {_TEST_PASSWORD}",
        )

        assert response.status_code == 503


# ===========================================================================
# 2. Auth gate applied to all other API endpoints
# ===========================================================================


class TestAuthGateAllEndpoints:
    """All /api/ endpoints must reject unauthenticated requests."""

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_get_job_status_no_auth_rejected(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, _secret = create_job(rows, remote_enabled=False)
        client = Client()
        response = client.get(f"/api/jobs/{job.pk}")
        assert response.status_code in (401, 403)

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_get_job_items_no_auth_rejected(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, _secret = create_job(rows, remote_enabled=False)
        client = Client()
        response = client.get(f"/api/jobs/{job.pk}/items")
        assert response.status_code in (401, 403)

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_post_item_structure_no_auth_rejected(self) -> None:
        from apps.core.enums import BatchItemStatus

        ghost_inchikey = "RDHQFKQIGNGIED-MRVPVSSYSA-N"
        rows = [{"raw": ghost_inchikey, "row_index": 0}]
        job, secret = create_job(rows, remote_enabled=False)
        item = job.items.filter(status=BatchItemStatus.NEED_STRUCTURE).first()
        if item is None:
            pytest.skip("No NEED_STRUCTURE item available")
        client = Client()
        response = client.post(
            f"/api/items/{item.pk}/structure",
            data=json.dumps({"structure": "CCO", "secret": secret}),
            content_type="application/json",
        )
        assert response.status_code in (401, 403)

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_post_exports_no_auth_rejected(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client()
        response = client.post(
            f"/api/jobs/{job.pk}/exports",
            data=json.dumps({"format": "csv", "secret": secret}),
            content_type="application/json",
        )
        assert response.status_code in (401, 403)

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_get_export_unknown_token_is_404(self) -> None:
        """Export download authorizes on the capability token alone.

        An unknown token returns 404 (not 401/403): the response must not reveal
        the auth model or whether a token exists. A valid token IS the
        authorization — see tests/test_export_token_capability.py for the
        positive path.
        """
        client = Client()
        response = client.get("/api/exports/fake-token-xyz")
        assert response.status_code == 404


# ===========================================================================
# 3. CSRF policy -- MEDIUM finding compensating control
# ===========================================================================


class TestCsrfPolicy:
    """[MEDIUM] @csrf_exempt on /api/jobs is safe because require_shared_access is present."""

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_no_csrf_token_no_bearer_is_rejected(self) -> None:
        """Without bearer token, auth gate rejects before CSRF check matters."""
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            "/api/jobs",
            data=json.dumps({"text_input": ASPIRIN_SMILES}),
            content_type="application/json",
        )
        assert response.status_code in (401, 403)

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_bearer_without_csrf_token_is_accepted(self) -> None:
        """Bearer-token requests don't need a CSRF token (no ambient cookie authority)."""
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            "/api/jobs",
            data=json.dumps({"text_input": ASPIRIN_SMILES}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {_TEST_PASSWORD}",
        )
        assert response.status_code == 202


# ===========================================================================
# 4. Result URL secret validation (Task 8, confirmed)
# ===========================================================================


class TestResultUrlSecret:
    @pytest.mark.django_db(transaction=True)
    def test_correct_secret_redirects_to_secretless_url(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client()
        response = client.get(f"/jobs/{job.pk}/{secret}/")
        assert response.status_code == 303
        assert response["Location"] == f"/jobs/{job.pk}/"

    @pytest.mark.django_db(transaction=True)
    def test_wrong_secret_gives_403(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, _secret = create_job(rows, remote_enabled=False)
        client = Client()
        response = client.get(f"/jobs/{job.pk}/wrongsecret/")
        assert response.status_code == 403

    @pytest.mark.django_db(transaction=True)
    def test_missing_job_gives_404(self) -> None:
        client = Client()
        fake_uuid = "00000000-0000-0000-0000-000000000099"
        response = client.get(f"/jobs/{fake_uuid}/anysecret/")
        assert response.status_code == 404

    @pytest.mark.django_db(transaction=True)
    def test_api_wrong_secret_gives_403(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, _secret = create_job(rows, remote_enabled=False)
        client = Client()
        # Pass wrong secret via header (query-string secrets now return 400)
        response = client.get(f"/api/jobs/{job.pk}", HTTP_X_JOB_SECRET="wrongsecret")
        # dev-open: auth gate passes, then per-job secret check gives 403
        assert response.status_code == 403

    @pytest.mark.django_db(transaction=True)
    @override_settings(SESSION_COOKIE_SECURE=True)
    def test_job_claim_sets_cookie_and_redirects_to_secretless_url(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client()

        response = client.get(f"/jobs/{job.pk}/claim/{secret}/", secure=True)

        assert response.status_code == 303
        assert response["Location"] == f"/jobs/{job.pk}/"
        job_cookies = [
            morsel
            for name, morsel in response.cookies.items()
            if name.startswith("cf_job_access_")
        ]
        assert len(job_cookies) == 1
        cookie = job_cookies[0]
        assert cookie["httponly"]
        assert cookie["secure"]
        assert cookie["samesite"] == "Lax"

    @pytest.mark.django_db(transaction=True)
    def test_secret_path_is_not_rendered_after_claim(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client()

        claim_response = client.get(f"/jobs/{job.pk}/claim/{secret}/")
        assert claim_response.status_code == 303
        response = client.get(f"/jobs/{job.pk}/")

        assert response.status_code == 200
        content = response.content.decode()
        assert secret not in content
        assert f"/jobs/{job.pk}/{secret}/" not in content
        assert f"/jobs/{job.pk}/claim/{secret}/" not in content

    @pytest.mark.django_db(transaction=True)
    def test_legacy_secret_url_sets_cookie_and_redirects_without_rendering_secret(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client()

        response = client.get(f"/jobs/{job.pk}/{secret}/")

        assert response.status_code == 303
        assert response["Location"] == f"/jobs/{job.pk}/"
        assert secret.encode() not in response.content
        job_cookies = [
            morsel
            for name, morsel in response.cookies.items()
            if name.startswith("cf_job_access_")
        ]
        assert len(job_cookies) == 1

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_claim_cookie_accesses_api_status_and_items_without_secret(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client()

        claim_response = client.get(f"/jobs/{job.pk}/claim/{secret}/")
        assert claim_response.status_code == 303

        status_response = client.get(f"/api/jobs/{job.pk}")
        items_response = client.get(f"/api/jobs/{job.pk}/items")

        assert status_response.status_code == 200
        assert status_response.json()["id"] == str(job.pk)
        assert items_response.status_code == 200
        assert items_response.json()["count"] >= 1

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_wrong_explicit_secret_fails_even_with_valid_job_cookie(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client()

        claim_response = client.get(f"/jobs/{job.pk}/claim/{secret}/")
        assert claim_response.status_code == 303
        # Pass wrong secret via header (query-string secrets now return 400)
        response = client.get(f"/api/jobs/{job.pk}", HTTP_X_JOB_SECRET="wrong")

        assert response.status_code == 403

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_job_cookie_cannot_be_reused_for_another_job(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        first_job, first_secret = create_job(rows, remote_enabled=False)
        second_job, _second_secret = create_job(rows, remote_enabled=False)
        client = Client()

        claim_response = client.get(f"/jobs/{first_job.pk}/claim/{first_secret}/")
        assert claim_response.status_code == 303
        response = client.get(f"/api/jobs/{second_job.pk}")

        assert response.status_code in (401, 403)

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_claim_cookie_creates_and_downloads_export_without_shared_auth(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client(enforce_csrf_checks=True)

        claim_response = client.get(f"/jobs/{job.pk}/claim/{secret}/")
        assert claim_response.status_code == 303
        csrf_response = client.get(f"/jobs/{job.pk}/")
        csrf_token = csrf_response.cookies["csrftoken"].value
        create_response = client.post(
            f"/api/jobs/{job.pk}/exports",
            data=json.dumps({"format": "csv"}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        assert create_response.status_code == 201

        token = create_response.json()["token"]
        download_response = client.get(f"/api/exports/{token}")

        assert download_response.status_code == 200
        assert "csv" in download_response.get("Content-Type", "")

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_job_cookie_export_post_without_csrf_is_rejected(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client(enforce_csrf_checks=True)

        claim_response = client.get(f"/jobs/{job.pk}/claim/{secret}/")
        assert claim_response.status_code == 303
        response = client.post(
            f"/api/jobs/{job.pk}/exports",
            data=json.dumps({"format": "csv"}),
            content_type="application/json",
        )

        assert response.status_code == 403

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_job_cookie_export_post_with_csrf_is_accepted(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client(enforce_csrf_checks=True)

        claim_response = client.get(f"/jobs/{job.pk}/claim/{secret}/")
        assert claim_response.status_code == 303
        csrf_response = client.get(f"/jobs/{job.pk}/")
        csrf_token = csrf_response.cookies["csrftoken"].value
        response = client.post(
            f"/api/jobs/{job.pk}/exports",
            data=json.dumps({"format": "csv"}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        assert response.status_code == 201

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_shared_session_job_create_without_csrf_is_rejected(self) -> None:
        client = Client(enforce_csrf_checks=True)
        login_page = client.get("/access/")
        csrf_token = login_page.cookies["csrftoken"].value
        login_response = client.post(
            "/access/",
            {"password": _TEST_PASSWORD, "next": "/"},
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        assert login_response.status_code == 303

        response = client.post(
            "/api/jobs",
            data=json.dumps({"text_input": ASPIRIN_SMILES, "remote_enabled": False}),
            content_type="application/json",
        )

        assert response.status_code == 403

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_shared_session_job_create_with_csrf_is_accepted(self) -> None:
        client = Client(enforce_csrf_checks=True)
        login_page = client.get("/access/")
        csrf_token = login_page.cookies["csrftoken"].value
        login_response = client.post(
            "/access/",
            {"password": _TEST_PASSWORD, "next": "/"},
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        assert login_response.status_code == 303
        csrf_response = client.get("/")
        csrf_token = csrf_response.cookies["csrftoken"].value

        response = client.post(
            "/api/jobs",
            data=json.dumps({"text_input": ASPIRIN_SMILES, "remote_enabled": False}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        assert response.status_code == 202

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_job_cookie_item_structure_post_without_csrf_is_rejected(self) -> None:
        rows = [{"raw": GHOST_INCHIKEY, "row_index": 0}]
        job, secret = create_job(rows, remote_enabled=False)
        item = job.items.first()
        assert item is not None
        client = Client(enforce_csrf_checks=True)

        claim_response = client.get(f"/jobs/{job.pk}/claim/{secret}/")
        assert claim_response.status_code == 303
        response = client.post(
            f"/api/items/{item.pk}/structure",
            data=json.dumps({"structure": GHOST_SMILES}),
            content_type="application/json",
        )

        assert response.status_code == 403

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_job_cookie_item_structure_post_with_csrf_is_accepted(self) -> None:
        rows = [{"raw": GHOST_INCHIKEY, "row_index": 0}]
        job, secret = create_job(rows, remote_enabled=False)
        item = job.items.first()
        assert item is not None
        client = Client(enforce_csrf_checks=True)

        claim_response = client.get(f"/jobs/{job.pk}/claim/{secret}/")
        assert claim_response.status_code == 303
        csrf_response = client.get(f"/jobs/{job.pk}/")
        csrf_token = csrf_response.cookies["csrftoken"].value
        response = client.post(
            f"/api/items/{item.pk}/structure",
            data=json.dumps({"structure": GHOST_SMILES}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        assert response.status_code == 200

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    @pytest.mark.parametrize(
        "auth_header",
        [
            {"HTTP_AUTHORIZATION": "Bearer wrong-token"},
            {"HTTP_X_SHARED_ACCESS_TOKEN": "wrong-token"},
        ],
    )
    def test_invalid_token_header_with_valid_job_cookie_still_requires_csrf(
        self,
        auth_header: dict[str, str],
    ) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client(enforce_csrf_checks=True)

        claim_response = client.get(f"/jobs/{job.pk}/claim/{secret}/")
        assert claim_response.status_code == 303
        response = client.post(
            f"/api/jobs/{job.pk}/exports",
            data=json.dumps({"format": "csv"}),
            content_type="application/json",
            **auth_header,
        )

        assert response.status_code == 403

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_shared_password_form_authenticates_browser_session(self) -> None:
        client = Client(enforce_csrf_checks=True)

        login_page = client.get("/access/")
        assert login_page.status_code == 200
        csrf_token = login_page.cookies["csrftoken"].value

        login_response = client.post(
            "/access/",
            {"password": _TEST_PASSWORD, "next": "/"},
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        assert login_response.status_code == 303
        assert login_response["Location"] == "/"
        response = client.get("/")
        assert response.status_code == 200

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_shared_password_login_cycles_session_key(self) -> None:
        from apps.core.security import SHARED_ACCESS_SESSION_KEY

        client = Client(enforce_csrf_checks=True)
        session = client.session
        session["pre_login_marker"] = "kept"
        session.save()
        old_session_key = session.session_key

        login_page = client.get("/access/")
        csrf_token = login_page.cookies["csrftoken"].value
        login_response = client.post(
            "/access/",
            {"password": _TEST_PASSWORD, "next": "/"},
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        assert login_response.status_code == 303
        assert client.session.session_key != old_session_key
        assert client.session.get("pre_login_marker") == "kept"
        assert client.session.get(SHARED_ACCESS_SESSION_KEY) is True

    @pytest.mark.django_db(transaction=True)
    @override_settings(
        SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH,
        SHARED_ACCESS_LOGIN_MAX_FAILURES=2,
        SHARED_ACCESS_LOGIN_WINDOW_SECONDS=60,
    )
    def test_shared_password_login_throttles_repeated_failures(self) -> None:
        client = Client(enforce_csrf_checks=True)
        ip = "198.51.100.77"

        first_page = client.get("/access/", REMOTE_ADDR=ip)
        first = client.post(
            "/access/",
            {"password": "wrong", "next": "/"},
            HTTP_X_CSRFTOKEN=first_page.cookies["csrftoken"].value,
            REMOTE_ADDR=ip,
        )
        second_page = client.get("/access/", REMOTE_ADDR=ip)
        second = client.post(
            "/access/",
            {"password": "wrong", "next": "/"},
            HTTP_X_CSRFTOKEN=second_page.cookies["csrftoken"].value,
            REMOTE_ADDR=ip,
        )
        blocked_page = client.get("/access/", REMOTE_ADDR=ip)
        blocked = client.post(
            "/access/",
            {"password": "wrong", "next": "/"},
            HTTP_X_CSRFTOKEN=blocked_page.cookies["csrftoken"].value,
            REMOTE_ADDR=ip,
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert blocked.status_code == 429

    def test_caddy_does_not_log_raw_claim_secret_paths(self) -> None:
        caddyfile = Path(__file__).resolve().parents[1] / "deploy/caddy/Caddyfile"
        content = caddyfile.read_text()

        assert "log_skip" in content
        assert "/claim/" in content

    def test_caddy_does_not_log_raw_legacy_secret_paths(self) -> None:
        caddyfile = Path(__file__).resolve().parents[1] / "deploy/caddy/Caddyfile"
        content = caddyfile.read_text()

        assert "jobLegacySecret" in content
        assert "^/jobs/[^/]+/[^/]+/?$" in content
        assert "^/jobs/[^/]+/[^/]+/items/?$" in content

    def test_caddy_does_not_log_raw_legacy_structure_secret_paths(self) -> None:
        caddyfile = Path(__file__).resolve().parents[1] / "deploy/caddy/Caddyfile"
        content = caddyfile.read_text()

        assert "itemLegacyStructureSecret" in content
        assert "^/items/[0-9]+/[^/]+/structure/?$" in content

    def test_caddy_active_health_check_uses_prod_safe_headers(self) -> None:
        caddyfile = Path(__file__).resolve().parents[1] / "deploy/caddy/Caddyfile"
        content = caddyfile.read_text()

        assert "health_headers" in content
        assert "Host {env.NGROK_DOMAIN}" in content
        assert "X-Forwarded-Proto https" in content

    def test_caddy_upload_limit_has_parseable_default(self) -> None:
        caddyfile = Path(__file__).resolve().parents[1] / "deploy/caddy/Caddyfile"
        content = caddyfile.read_text()

        assert "{$MAX_UPLOAD_MB:100}mb" in content
        assert "{env.MAX_UPLOAD_MB}mb" not in content

    def test_caddy_access_logs_drop_secret_bearing_headers(self) -> None:
        caddyfile = Path(__file__).resolve().parents[1] / "deploy/caddy/Caddyfile"
        content = caddyfile.read_text()

        assert "format filter" in content
        assert "wrap json" in content
        for header in [
            "request>headers>Authorization delete",
            "request>headers>Cookie delete",
            "request>headers>X-Job-Secret delete",
            "request>headers>X-Shared-Access-Token delete",
        ]:
            assert header in content


# ===========================================================================
# 5. Production settings
# ===========================================================================


class TestProdSettings:
    def test_prod_has_secure_proxy_ssl_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _reload_prod_settings(monkeypatch, {})
        assert getattr(mod, "SECURE_PROXY_SSL_HEADER", None) is not None

    def test_prod_session_cookie_secure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _reload_prod_settings(monkeypatch, {})
        assert getattr(mod, "SESSION_COOKIE_SECURE", False) is True

    def test_prod_csrf_cookie_secure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _reload_prod_settings(monkeypatch, {})
        assert getattr(mod, "CSRF_COOKIE_SECURE", False) is True

    def test_prod_hsts_one_year(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _reload_prod_settings(monkeypatch, {})
        assert getattr(mod, "SECURE_HSTS_SECONDS", 0) >= 31536000

    def test_prod_requires_shared_access_hash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from django.core.exceptions import ImproperlyConfigured

        with pytest.raises(ImproperlyConfigured):
            _reload_prod_settings(monkeypatch, {"SHARED_ACCESS_PASSWORD_HASH": ""})

    def test_prod_session_cookie_httponly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _reload_prod_settings(monkeypatch, {})
        assert getattr(mod, "SESSION_COOKIE_HTTPONLY", False) is True

    def test_prod_x_frame_options_deny(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _reload_prod_settings(monkeypatch, {})
        assert getattr(mod, "X_FRAME_OPTIONS", "") == "DENY"


# ===========================================================================
# 6. Base settings -- circuit breaker settings present
# ===========================================================================


class TestBaseSettingsCircuit:
    def test_remote_circuit_threshold_present(self, settings: object) -> None:
        assert hasattr(settings, "REMOTE_CIRCUIT_THRESHOLD")
        assert isinstance(settings.REMOTE_CIRCUIT_THRESHOLD, int)  # type: ignore[union-attr]
        assert settings.REMOTE_CIRCUIT_THRESHOLD > 0  # type: ignore[union-attr]

    def test_remote_circuit_reset_seconds_present(self, settings: object) -> None:
        assert hasattr(settings, "REMOTE_CIRCUIT_RESET_SECONDS")
        assert isinstance(settings.REMOTE_CIRCUIT_RESET_SECONDS, int)  # type: ignore[union-attr]
        assert settings.REMOTE_CIRCUIT_RESET_SECONDS > 0  # type: ignore[union-attr]


# ===========================================================================
# 7. Docker Compose port exposure
# ===========================================================================


class TestDockerComposePortExposure:
    COMPOSE_FILE = str(Path(__file__).resolve().parents[1] / "docker-compose.yml")

    def _load_compose(self) -> dict:
        with open(self.COMPOSE_FILE) as f:
            return yaml.safe_load(f)

    def test_db_has_no_host_ports(self) -> None:
        data = self._load_compose()
        db = data["services"]["db"]
        assert "ports" not in db, "db service must not publish host ports"

    def test_web_has_no_host_ports(self) -> None:
        data = self._load_compose()
        web = data["services"]["web"]
        assert "ports" not in web, "web service must not publish host ports"

    def test_worker_has_no_host_ports(self) -> None:
        data = self._load_compose()
        worker = data["services"]["worker"]
        assert "ports" not in worker, "worker service must not publish host ports"

    def test_backup_has_no_host_ports(self) -> None:
        data = self._load_compose()
        backup = data["services"]["backup"]
        assert "ports" not in backup, "backup service must not publish host ports"

    def test_proxy_publishes_80_and_443(self) -> None:
        data = self._load_compose()
        proxy = data["services"]["proxy"]
        ports = proxy.get("ports", [])
        port_strings = [str(p) for p in ports]
        assert any("80" in p for p in port_strings), "proxy should publish port 80"
        assert any("443" in p for p in port_strings), "proxy should publish port 443"

    def test_web_gunicorn_access_log_disabled_but_error_log_kept(self) -> None:
        data = self._load_compose()
        command = data["services"]["web"]["command"]
        assert "--access-logfile=/dev/null" in command
        assert "--access-logfile=-" not in command
        assert "--error-logfile=-" in command


# ===========================================================================
# 8. Upload safety
# ===========================================================================


class TestUploadSafety:
    def test_oversize_upload_raises(self) -> None:
        with pytest.raises(UploadTooLargeError):
            check_upload_size(content_length=200 * 1024 * 1024, max_mb=100)

    def test_within_size_limit_passes(self) -> None:
        check_upload_size(content_length=50 * 1024 * 1024, max_mb=100)

    def test_exact_limit_passes(self) -> None:
        check_upload_size(content_length=100 * 1024 * 1024, max_mb=100)

    def test_one_byte_over_limit_raises(self) -> None:
        with pytest.raises(UploadTooLargeError):
            check_upload_size(content_length=100 * 1024 * 1024 + 1, max_mb=100)

    def test_overlong_structure_raises(self) -> None:
        long_structure = "C" * (MAX_STRUCTURE_LEN + 1)
        with pytest.raises(RowTooLongError):
            check_structure_length(long_structure)

    def test_ok_structure_length_passes(self) -> None:
        check_structure_length("CC(=O)O")

    def test_overlong_sdf_property_raises(self) -> None:
        big_value = "x" * (MAX_SDF_PROPERTY_LEN + 1)
        with pytest.raises(SdfPropertyTooLargeError):
            check_sdf_property_length(big_value)

    def test_ok_sdf_property_length_passes(self) -> None:
        check_sdf_property_length("some property value")

    def test_zip_bomb_raises(self) -> None:
        bomb = _make_zip_bomb()
        with pytest.raises(DecompressionBombError):
            check_decompression_ratio(bomb, max_ratio=10.0)

    def test_zip_bomb_forged_metadata_raises(self) -> None:
        """Zip-bomb with forged central-directory file_size=0 must still be caught.

        This tests that check_decompression_ratio does NOT trust info.file_size
        (attacker-controlled metadata). We simulate an archive whose infolist
        reports file_size=0 for every member while open() actually yields large
        payloads. A metadata-trusting guard would see zero decompressed bytes and
        pass silently. The streaming guard must detect the real bytes and raise.
        """
        import unittest.mock as mock

        # Simulate an archive with two entries each reporting file_size=0 but
        # whose real content totals well above (compressed_size * max_ratio).
        fake_compressed_size = 500  # bytes — sets the limit; ratio 10x → 5000 byte ceiling
        max_ratio = 10.0
        # Each fake member yields 3000 bytes → 6000 total > 5000 limit
        large_payload = b"\x00" * 3000

        fake_info1 = zipfile.ZipInfo("a.txt")
        fake_info1.file_size = 0  # attacker lies: declares 0

        fake_info2 = zipfile.ZipInfo("b.txt")
        fake_info2.file_size = 0  # attacker lies: declares 0

        def fake_infolist(self_zf):  # type: ignore[no-untyped-def]
            return [fake_info1, fake_info2]

        def fake_open(self_zf, name_or_info, mode="r", pwd=None, *, force_zip64=False):  # type: ignore[no-untyped-def]
            return io.BytesIO(large_payload)

        # Build a minimal valid-looking bytes blob of the right size so that
        # compressed_size == fake_compressed_size inside check_decompression_ratio.
        fake_zip_data = b"PK\x05\x06" + b"\x00" * (fake_compressed_size - 4)

        with (
            mock.patch.object(zipfile.ZipFile, "infolist", fake_infolist),
            mock.patch.object(zipfile.ZipFile, "open", fake_open),
            mock.patch("zipfile.ZipFile.__init__", lambda self, *a, **kw: None),
            mock.patch("zipfile.ZipFile.__exit__", lambda self, *a: None),
            mock.patch("zipfile.ZipFile.__enter__", lambda self: self),
        ):
            with pytest.raises(DecompressionBombError):
                check_decompression_ratio(fake_zip_data, max_ratio=max_ratio)

    def test_normal_zip_passes(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("data.txt", "Hello, world!")
        data = buf.getvalue()
        result = check_decompression_ratio(data, max_ratio=100.0)
        assert b"Hello" in result

    def test_bad_zip_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            check_decompression_ratio(b"not-a-zip-file", max_ratio=100.0)

    def test_error_message_does_not_contain_data(self) -> None:
        """Upload errors must not leak structure content."""
        long_structure = "SECRET" * 20_000
        try:
            check_structure_length(long_structure)
            raise AssertionError("Should have raised")
        except RowTooLongError as exc:
            assert "SECRET" not in str(exc)


# ===========================================================================
# 9. Secret / log redaction helper
# ===========================================================================


class TestLogRedaction:
    def test_short_value_passes_through(self) -> None:
        assert redact_for_log("OK") == "OK"
        assert redact_for_log("401") == "401"

    def test_long_value_is_redacted(self) -> None:
        long_val = "s" * 100
        result = redact_for_log(long_val)
        assert "REDACTED" in result
        assert long_val not in result

    def test_secret_hash_is_redacted(self) -> None:
        fake_secret = "pbkdf2_sha256$600000$salt$hashhashhashhashhashhashhashhash"
        result = redact_for_log(fake_secret)
        assert "REDACTED" in result

    def test_smiles_structure_is_redacted(self) -> None:
        smiles = "CC(=O)Oc1ccccc1C(=O)O" * 10
        result = redact_for_log(smiles)
        assert "REDACTED" in result

    def test_redaction_includes_length_hint(self) -> None:
        val = "x" * 200
        result = redact_for_log(val)
        assert "200" in result

    def test_custom_max_chars(self) -> None:
        result = redact_for_log("hello", max_chars=3)
        assert "REDACTED" in result

    def test_exact_max_chars_not_redacted(self) -> None:
        result = redact_for_log("abc", max_chars=3)
        assert result == "abc"


class TestSecurityImplementationGuards:
    def test_api_does_not_import_private_export_helpers(self) -> None:
        api_file = Path(__file__).resolve().parents[1] / "apps/batches/api.py"
        content = api_file.read_text()

        assert "_find_existing_token" not in content
        assert "_write_export_file" not in content
        assert "_meta_path" not in content
        assert "_create_export_without_secret" not in content
        assert "create_export_for_authorized_job" in content

    def test_quota_advisory_lock_keys_are_stable_and_distinct(self) -> None:
        from apps.core.security import quota_advisory_lock_keys

        first = quota_advisory_lock_keys("job_create", "principal-a", "127.0.0.1")
        again = quota_advisory_lock_keys("job_create", "principal-a", "127.0.0.1")
        different_principal = quota_advisory_lock_keys(
            "job_create", "principal-b", "127.0.0.1"
        )
        different_ip = quota_advisory_lock_keys("job_create", "principal-a", "127.0.0.2")

        assert first == again
        assert first != different_principal
        assert first != different_ip
        assert all(-(2**31) <= value < 2**31 for value in first)

    def test_invalid_bearer_header_does_not_spoof_session_principal(self) -> None:
        from django.contrib.auth.hashers import make_password
        from django.test import RequestFactory

        from apps.core.security import SHARED_ACCESS_SESSION_KEY, _principal

        request = RequestFactory().post(
            "/api/jobs",
            REMOTE_ADDR="198.51.100.20",
            HTTP_AUTHORIZATION="Bearer attacker-controlled",
        )
        request.session = types.SimpleNamespace(
            session_key="stable-session",
            get=lambda key, default=None: (
                True if key == SHARED_ACCESS_SESSION_KEY else default
            ),
        )

        with override_settings(SHARED_ACCESS_PASSWORD_HASH=make_password("correct")):
            assert _principal(request).startswith("session:")

    @pytest.mark.django_db(transaction=True)
    @override_settings(INGEST_DEFER_HEAVY_PROCESSING=False)
    def test_create_job_under_quota_processes_items_after_quota_transaction(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Service-level test: create_job_under_quota runs processing outside the
        quota transaction.  Requires INGEST_DEFER_HEAVY_PROCESSING=False so the
        guard inside create_job_under_quota does not raise."""
        from django.db import connection
        from django.test import RequestFactory

        from apps.batches import services
        from apps.batches.models import BatchQuotaEvent
        from apps.core.security import create_job_under_quota

        request = RequestFactory().post("/api/jobs", REMOTE_ADDR="198.51.100.20")
        rows = list(parse_text_input(ASPIRIN_SMILES))
        original_create_job_records = services.create_job_records
        original_process_job_items = services.process_job_items
        create_atomic_states: list[bool] = []
        process_atomic_states: list[bool] = []

        def spy_create_job_records(
            rows_arg: list[dict[str, object]],
            *,
            remote_enabled: bool,
            remote_principal: str = "system",
        ):
            create_atomic_states.append(connection.in_atomic_block)
            assert remote_principal.startswith("ip:")
            return original_create_job_records(
                rows_arg,
                remote_enabled=remote_enabled,
                remote_principal=remote_principal,
            )

        def spy_process_job_items(job):
            process_atomic_states.append(connection.in_atomic_block)
            return original_process_job_items(job)

        monkeypatch.setattr(services, "create_job_records", spy_create_job_records)
        monkeypatch.setattr(services, "process_job_items", spy_process_job_items)

        job, secret, quota_error = create_job_under_quota(
            request,
            rows,
            remote_enabled=False,
        )

        assert quota_error is None
        assert job is not None
        assert secret
        assert create_atomic_states == [True]
        assert process_atomic_states == [False]
        assert BatchQuotaEvent.objects.filter(
            kind=BatchQuotaEvent.KIND_JOB_CREATE,
            row_count=len(rows),
        ).exists()


# ===========================================================================
# 10. Referrer-Policy: no-referrer header (defence-in-depth for URL secrets)
# ===========================================================================


class TestReferrerPolicy:
    """Responses must carry Referrer-Policy: no-referrer for defense in depth."""

    @pytest.mark.django_db(transaction=True)
    def test_result_page_has_no_referrer_policy(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client()
        claim_response = client.get(f"/jobs/{job.pk}/claim/{secret}/")
        assert claim_response.status_code == 303
        response = client.get(f"/jobs/{job.pk}/")
        assert response.status_code == 200
        assert response.headers.get("Referrer-Policy") == "no-referrer", (
            f"Expected Referrer-Policy: no-referrer, got: {response.headers.get('Referrer-Policy')}"
        )

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_api_response_has_no_referrer_policy(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client()
        # Use X-Job-Secret header (query-string secrets now return 400)
        response = client.get(
            f"/api/jobs/{job.pk}",
            HTTP_AUTHORIZATION=f"Bearer {_TEST_PASSWORD}",
            HTTP_X_JOB_SECRET=secret,
        )
        assert response.status_code == 200
        assert response.headers.get("Referrer-Policy") == "no-referrer", (
            f"Expected Referrer-Policy: no-referrer, got: {response.headers.get('Referrer-Policy')}"
        )


# ===========================================================================
# 11. X-Job-Secret header support (keeps secret out of query-string logs)
# ===========================================================================


class TestXJobSecretHeader:
    """The API accepts the per-job secret via the X-Job-Secret request header
    or body["secret"]; ?secret= query params are rejected with HTTP 400."""

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_correct_header_secret_gives_200(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client()
        # Pass the secret in the header only — NOT in query string or body.
        response = client.get(
            f"/api/jobs/{job.pk}",
            HTTP_AUTHORIZATION=f"Bearer {_TEST_PASSWORD}",
            HTTP_X_JOB_SECRET=secret,
        )
        assert response.status_code == 200, (
            f"Expected 200 with correct X-Job-Secret header, got {response.status_code}: "
            f"{response.content}"
        )

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_job_secret_header_allows_job_status_without_shared_bearer(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client(enforce_csrf_checks=True)

        response = client.get(
            f"/api/jobs/{job.pk}",
            HTTP_X_JOB_SECRET=secret,
        )

        assert response.status_code == 200, response.content

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_job_secret_header_allows_export_without_shared_bearer(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client(enforce_csrf_checks=True)

        response = client.post(
            f"/api/jobs/{job.pk}/exports",
            data=json.dumps({"format": "csv"}),
            content_type="application/json",
            HTTP_X_JOB_SECRET=secret,
        )

        assert response.status_code == 201, response.content

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_wrong_header_secret_gives_403(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, _secret = create_job(rows, remote_enabled=False)
        client = Client()
        response = client.get(
            f"/api/jobs/{job.pk}",
            HTTP_AUTHORIZATION=f"Bearer {_TEST_PASSWORD}",
            HTTP_X_JOB_SECRET="definitely-wrong-secret",
        )
        assert response.status_code == 403, (
            f"Expected 403 with wrong X-Job-Secret header, got {response.status_code}"
        )

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_query_secret_rejected_with_400(self) -> None:
        """Query-string secrets are no longer accepted; API returns 400 with
        a message directing the caller to use the X-Job-Secret header."""
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client()
        response = client.get(
            f"/api/jobs/{job.pk}?secret={secret}",
            HTTP_AUTHORIZATION=f"Bearer {_TEST_PASSWORD}",
        )
        assert response.status_code == 400, (
            f"Expected 400 for ?secret= query param, got {response.status_code}"
        )
        assert "X-Job-Secret" in response.json()["error"]

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_header_takes_priority_over_query(self) -> None:
        """X-Job-Secret header is checked first; a wrong query param is ignored
        when the header carries the correct secret."""
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client()
        response = client.get(
            f"/api/jobs/{job.pk}?secret=wrong-query-param",
            HTTP_AUTHORIZATION=f"Bearer {_TEST_PASSWORD}",
            HTTP_X_JOB_SECRET=secret,
        )
        assert response.status_code == 200, (
            f"Expected 200 when header is correct and query is wrong, got {response.status_code}"
        )


# ===========================================================================
# 12. Web UI shared-access gate (HIGH #4)
# ===========================================================================


class TestWebGate:
    """[HIGH #4] submit_view (/) and stats_view (/stats/) must be gated in
    locked mode and open in dev-open mode."""

    # --- locked mode: unauthenticated requests are rejected ---

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_submit_unauthenticated_locked_mode_rejected(self) -> None:
        client = Client()
        response = client.get("/")
        assert response.status_code in (302, 403), (
            f"Expected 302/403 for unauthenticated GET / in locked mode, "
            f"got {response.status_code}"
        )

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_stats_unauthenticated_locked_mode_rejected(self) -> None:
        client = Client()
        response = client.get("/stats/")
        assert response.status_code in (302, 403), (
            f"Expected 302/403 for unauthenticated GET /stats/ in locked mode, "
            f"got {response.status_code}"
        )

    # --- locked mode: authenticated requests via Bearer token pass ---

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_submit_valid_bearer_locked_mode_passes(self) -> None:
        client = Client()
        response = client.get(
            "/",
            HTTP_AUTHORIZATION=f"Bearer {_TEST_PASSWORD}",
        )
        assert response.status_code == 200, (
            f"Expected 200 for authenticated GET / in locked mode, "
            f"got {response.status_code}"
        )

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_stats_valid_bearer_locked_mode_passes(self) -> None:
        client = Client()
        response = client.get(
            "/stats/",
            HTTP_AUTHORIZATION=f"Bearer {_TEST_PASSWORD}",
        )
        assert response.status_code == 200, (
            f"Expected 200 for authenticated GET /stats/ in locked mode, "
            f"got {response.status_code}"
        )

    # --- dev-open mode: no hash set → pages remain public ---

    @pytest.mark.django_db(transaction=True)
    def test_submit_dev_open_mode_is_public(self) -> None:
        client = Client()
        response = client.get("/")
        assert response.status_code == 200, (
            f"Expected 200 in dev-open mode for GET /, got {response.status_code}"
        )

    @pytest.mark.django_db(transaction=True)
    def test_stats_dev_open_mode_is_public(self) -> None:
        client = Client()
        response = client.get("/stats/")
        assert response.status_code == 200, (
            f"Expected 200 in dev-open mode for GET /stats/, got {response.status_code}"
        )

    # --- per-job result pages remain accessible by job secret (not behind gate) ---

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_job_detail_accessible_by_job_secret_in_locked_mode(self) -> None:
        """Legacy per-job secret links exchange to a scoped job cookie."""
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client()
        response = client.get(f"/jobs/{job.pk}/{secret}/")
        assert response.status_code == 303, (
            f"Expected 303 for job detail page with correct secret in locked mode, "
            f"got {response.status_code}"
        )
        assert response["Location"] == f"/jobs/{job.pk}/"


# ===========================================================================
# 13. Upload size guard wired into the real ingest path (MEDIUM #5)
# ===========================================================================


class TestUploadSizeGuardOnRealPath:
    """[MEDIUM #5] check_upload_size must be called before reading the upload
    in submit_view so an oversize file is rejected without materializing it."""

    @pytest.mark.django_db(transaction=True)
    def test_oversize_file_upload_returns_413_or_400(self) -> None:
        """An upload whose reported size exceeds MAX_UPLOAD_MB must be rejected
        with 413 (or 400) — no 500, no content leakage in the response.

        Django's test client re-encodes multipart data so the actual file size
        reported at the view layer matches the real content size, not any
        pre-set .size attribute.  We therefore patch check_upload_size inside
        views to simulate a size-check failure without allocating hundreds of MB,
        while verifying that the wiring is in place (guard is called and its
        exception is handled correctly).
        """
        import unittest.mock as mock

        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.core.security import UploadTooLargeError

        tiny_content = b"CC(=O)O\n"
        uploaded = SimpleUploadedFile("test.txt", tiny_content, content_type="text/plain")

        # Patch check_upload_size in the views module to raise unconditionally,
        # simulating an oversize upload without needing a real large file.
        with mock.patch(
            "apps.batches.views.check_upload_size",
            side_effect=UploadTooLargeError("Upload size exceeds limit of 100 MB."),
        ):
            client = Client()
            response = client.post(
                "/",
                {"input_file": uploaded, "text_input": "", "remote_enabled": ""},
            )

        assert response.status_code in (400, 413), (
            f"Expected 413/400 for oversize upload, got {response.status_code}"
        )
        # Must not leak file content in the response body.
        assert b"CC(=O)O" not in response.content, (
            "Response must not echo back file content on oversize error"
        )

    @pytest.mark.django_db(transaction=True)
    def test_overlong_row_results_in_invalid_item_others_processed(self) -> None:
        """A row whose structure string exceeds MAX_STRUCTURE_LEN must be
        recorded as INVALID_INPUT; other rows in the same job must still be
        processed normally.

        The web submit route now uses the deferred path, so overlong-row
        detection only runs when the durable worker processes the job.  This
        test stages the job via the route, then drains it with run_pending_jobs
        before checking the final item statuses.
        """
        from apps.batches.models import BatchJob
        from apps.batches.services import run_pending_jobs
        from apps.core.enums import BatchItemStatus
        from apps.core.security import MAX_STRUCTURE_LEN

        # Build a multi-row submission: one good row + one over-limit row.
        long_structure = "C" * (MAX_STRUCTURE_LEN + 1)
        text_input = f"{ASPIRIN_SMILES}\n{long_structure}"

        client = Client()
        response = client.post(
            "/",
            {"text_input": text_input, "remote_enabled": ""},
            follow=False,
        )
        # Should redirect to a job page (not crash).
        assert response.status_code == 302, (
            f"Expected 302 redirect after submit with overlong row, "
            f"got {response.status_code}"
        )
        location = response["Location"]
        assert location.startswith("/jobs/"), (
            f"Redirect should go to /jobs/..., got {location}"
        )
        # Follow the claim redirect to confirm the job was created and the
        # resulting secretless page is accessible by job cookie.
        claim_response = client.get(location)
        assert claim_response.status_code == 303
        response2 = client.get(claim_response["Location"])
        assert response2.status_code == 200

        # Drain the deferred job with the durable worker.
        parts = location.strip("/").split("/")
        job_id = parts[1]
        run_pending_jobs("test-worker", max_jobs=10)

        # Inspect the DB: the aspirin row must be present and processed;
        # the overlong row must be INVALID_INPUT.
        job = BatchJob.objects.get(pk=job_id)
        items = list(job.items.order_by("row_order"))
        assert len(items) == 2, f"Expected 2 items, got {len(items)}"

        # The aspirin item should NOT be invalid.
        aspirin_item = items[0]
        assert aspirin_item.status != BatchItemStatus.INVALID_INPUT, (
            f"Aspirin row should not be INVALID_INPUT, got {aspirin_item.status}"
        )

        # The overlong row item should be INVALID_INPUT.
        overlong_item = items[1]
        assert overlong_item.status == BatchItemStatus.INVALID_INPUT, (
            f"Overlong row should be INVALID_INPUT, got {overlong_item.status}"
        )


# ===========================================================================
# 14. API body-size guard + overlong row via API (Fix #1/#2)
# ===========================================================================


class TestApiIngestGuards:
    """[Fix #1/#2] API path must enforce both body-size and per-row length guards."""

    @pytest.mark.django_db(transaction=True)
    def test_oversize_api_body_returns_413(self) -> None:
        """A text_input whose UTF-8 byte length exceeds MAX_UPLOAD_MB must be
        rejected with 413 (or 400) — the rows must NOT be processed."""
        import unittest.mock as mock

        from apps.core.security import UploadTooLargeError

        # Patch check_upload_size in api module to raise unconditionally,
        # simulating an oversize body without allocating hundreds of MB.
        with mock.patch(
            "apps.batches.api.check_upload_size",
            side_effect=UploadTooLargeError("Upload size exceeds limit of 100 MB."),
        ):
            client = Client()
            response = client.post(
                "/api/jobs",
                data=json.dumps({"text_input": ASPIRIN_SMILES}),
                content_type="application/json",
            )

        assert response.status_code in (400, 413), (
            f"Expected 413/400 for oversize API body, got {response.status_code}"
        )
        # Must not leak content in the error response.
        data = response.json()
        assert "error" in data
        assert ASPIRIN_SMILES not in data["error"], (
            "Error response must not echo back structure content"
        )

    @pytest.mark.django_db(transaction=True)
    def test_overlong_row_via_service_is_invalid_with_bounded_original(self) -> None:
        """An over-limit row submitted via the service layer must become INVALID_INPUT.

        The item's original_input must contain a recognisable prefix of the
        submitted text (not a synthetic sentinel) plus a truncation marker, and
        the message must state the length limit.  Other rows in the same job
        must be processed normally.

        Note: the HTTP route now always uses the deferred path, so heavy
        processing (including overlong-row detection) only runs in the worker.
        This test exercises the service layer (create_job_records +
        process_job_items) directly, which is the correct layer for testing
        per-item business rules.
        """
        from apps.batches.ingest import parse_text_input
        from apps.batches.services import create_job_records, process_job_items
        from apps.core.enums import BatchItemStatus
        from apps.core.security import MAX_STRUCTURE_LEN

        long_structure = "C" * (MAX_STRUCTURE_LEN + 1)
        text_input = f"{ASPIRIN_SMILES}\n{long_structure}"

        rows = list(parse_text_input(text_input))
        job, _secret = create_job_records(rows, remote_enabled=False)
        process_job_items(job)

        items = list(job.items.order_by("row_order"))
        assert len(items) == 2

        # Aspirin row must NOT be invalid.
        aspirin_item = items[0]
        assert aspirin_item.status != BatchItemStatus.INVALID_INPUT, (
            f"Aspirin row should not be INVALID_INPUT, got {aspirin_item.status}"
        )

        # Overlong row must be INVALID_INPUT.
        overlong_item = items[1]
        assert overlong_item.status == BatchItemStatus.INVALID_INPUT, (
            f"Overlong row should be INVALID_INPUT, got {overlong_item.status}"
        )
        # original_input must contain a real prefix, NOT the old sentinel.
        assert "__INVALID__ROW_TOO_LONG__" not in overlong_item.original_input, (
            "original_input must not be the synthetic sentinel"
        )
        # Must start with the actual submitted characters.
        assert overlong_item.original_input.startswith("C"), (
            "original_input must start with the real submitted content"
        )
        # Must contain the truncation marker.
        assert "truncated" in overlong_item.original_input, (
            "original_input must contain a truncation marker"
        )
        # message must mention the limit.
        assert overlong_item.message, "message must be set for overlong row"
        assert "limit" in overlong_item.message.lower() or "exceed" in overlong_item.message.lower(), (
            f"message should state the length limit, got: {overlong_item.message}"
        )


# ===========================================================================
# 15. Job-create rate limit vs worker row-count accounting (kind split)
# ===========================================================================


class TestJobCreateKindSplit:
    """[MEDIUM] The worker's row-count accounting event must NOT consume a
    per-minute job-create rate slot.

    After H-04, every job-create writes TWO BatchQuotaEvent rows: a request-time
    reservation (row_count=0) and the worker's real-row-count accounting event.
    The per-minute rate limiter COUNTs job-create events, so if both share the
    same kind, the worker event halves the effective rate. The fix gives the
    worker event a DISTINCT kind (KIND_JOB_ROWS) so the per-minute COUNT sees
    exactly one event per job, while the daily-row-cap SUM still sees both kinds.
    """

    @pytest.mark.django_db(transaction=True)
    @override_settings(
        SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH,
        JOB_CREATE_RATE_LIMIT_PER_MINUTE=2,
    )
    def test_worker_rowcount_event_does_not_consume_rate_slot(self) -> None:
        """One job created via the spool API path + drained by the worker must
        leave exactly ONE KIND_JOB_CREATE reservation, so a SECOND job in the
        same minute still succeeds (202) under a limit of 2.

        Pre-fix this FAILS: the worker writes a second KIND_JOB_CREATE event, so
        the per-minute COUNT hits 2 and the second request is rejected (429).
        """
        from apps.batches.models import BatchQuotaEvent
        from apps.batches.services import run_pending_jobs

        client = Client()
        auth = {"HTTP_AUTHORIZATION": f"Bearer {_TEST_PASSWORD}"}

        # First job via the spool API path (text/paste now uses the spool too).
        first = client.post(
            "/api/jobs",
            data=json.dumps({"text_input": ASPIRIN_SMILES, "remote_enabled": False}),
            content_type="application/json",
            **auth,
        )
        assert first.status_code == 202, (
            f"First job-create should succeed (202), got {first.status_code}: {first.content}"
        )

        # Drain the worker so it stages the artifact and writes its row-count
        # accounting event within the same minute window.
        run_pending_jobs("test-worker", max_jobs=10)

        # The worker event must NOT be a KIND_JOB_CREATE reservation.
        job_create_count = BatchQuotaEvent.objects.filter(
            kind=BatchQuotaEvent.KIND_JOB_CREATE,
        ).count()
        assert job_create_count == 1, (
            "Exactly one KIND_JOB_CREATE reservation must exist per job after the "
            f"worker drains; got {job_create_count} (the worker event must use "
            "KIND_JOB_ROWS, not KIND_JOB_CREATE)."
        )
        # The worker's real row-count must be recorded under KIND_JOB_ROWS.
        assert BatchQuotaEvent.objects.filter(
            kind=BatchQuotaEvent.KIND_JOB_ROWS,
            row_count__gt=0,
        ).exists(), "Worker must record the real row count under KIND_JOB_ROWS"

        # Second job in the same minute must still succeed: only 1 job-create
        # reservation exists so far, and the limit is 2.
        second = client.post(
            "/api/jobs",
            data=json.dumps({"text_input": ASPIRIN_SMILES, "remote_enabled": False}),
            content_type="application/json",
            **auth,
        )
        assert second.status_code == 202, (
            "Second job-create in the same minute must succeed (202) — the worker "
            f"row-count event must not consume a rate slot; got {second.status_code}: "
            f"{second.content}"
        )

    @pytest.mark.django_db(transaction=True)
    @override_settings(
        SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH,
        JOB_CREATE_DAILY_ROW_CAP=1,
    )
    def test_daily_row_cap_still_enforced_after_kind_split(self) -> None:
        """The daily row cap must still be enforced by the worker after the kind
        split. With a cap of 1 row and a multi-row job, the worker (which sums
        over BOTH kinds) must park the job terminal-FAILED rather than staging it.
        """
        from apps.batches.models import BatchJob
        from apps.batches.services import run_pending_jobs
        from apps.core.enums import BatchJobStatus, BatchProcessingState

        client = Client()
        auth = {"HTTP_AUTHORIZATION": f"Bearer {_TEST_PASSWORD}"}

        # A two-row submission against a cap of 1 row.
        text_input = f"{ASPIRIN_SMILES}\n{ASPIRIN_SMILES}"
        response = client.post(
            "/api/jobs",
            data=json.dumps({"text_input": text_input, "remote_enabled": False}),
            content_type="application/json",
            **auth,
        )
        assert response.status_code == 202, (
            f"Job create should be staged (202), got {response.status_code}: {response.content}"
        )
        job_id = response.json()["job_id"]

        # The worker stages the artifact, discovers the real row count exceeds
        # the daily cap, and parks the job terminal-FAILED.
        run_pending_jobs("test-worker", max_jobs=10)

        job = BatchJob.objects.get(pk=job_id)
        assert job.status == BatchJobStatus.FAILED, (
            f"Daily row cap must still park the job FAILED; got status={job.status}"
        )
        assert job.processing_state == BatchProcessingState.FAILED, (
            f"processing_state must be FAILED; got {job.processing_state}"
        )
        assert "quota" in (job.processing_error or "").lower(), (
            f"processing_error must record the quota reason; got: {job.processing_error}"
        )
