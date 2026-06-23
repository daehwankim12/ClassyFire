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
import os
import sys
import types
import zipfile

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
    UploadTooLargeError,
    check_decompression_ratio,
    check_sdf_property_length,
    check_structure_length,
    check_upload_size,
    redact_for_log,
)

ASPIRIN_SMILES = "CC(=O)Oc1ccccc1C(=O)O"

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
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("config.settings") or mod_name == "apps.core.settings":
            del sys.modules[mod_name]
    return importlib.import_module("config.settings.prod")


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
    def test_post_api_jobs_valid_bearer_returns_201(self) -> None:
        client = Client()
        response = client.post(
            "/api/jobs",
            data=json.dumps({"text_input": ASPIRIN_SMILES}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {_TEST_PASSWORD}",
        )
        assert response.status_code == 201, (
            f"Expected 201 with valid bearer, got {response.status_code}: {response.content}"
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


# ===========================================================================
# 2. Auth gate applied to all other API endpoints
# ===========================================================================


class TestAuthGateAllEndpoints:
    """All /api/ endpoints must reject unauthenticated requests."""

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_get_job_status_no_auth_rejected(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client()
        response = client.get(f"/api/jobs/{job.pk}?secret={secret}")
        assert response.status_code in (401, 403)

    @pytest.mark.django_db(transaction=True)
    @override_settings(SHARED_ACCESS_PASSWORD_HASH=_TEST_HASH)
    def test_get_job_items_no_auth_rejected(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client()
        response = client.get(f"/api/jobs/{job.pk}/items?secret={secret}")
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
    def test_get_export_no_auth_rejected(self) -> None:
        client = Client()
        response = client.get("/api/exports/fake-token-xyz")
        assert response.status_code in (401, 403)


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
        assert response.status_code == 201


# ===========================================================================
# 4. Result URL secret validation (Task 8, confirmed)
# ===========================================================================


class TestResultUrlSecret:
    @pytest.mark.django_db(transaction=True)
    def test_correct_secret_gives_200(self) -> None:
        rows = list(parse_text_input(ASPIRIN_SMILES))
        job, secret = create_job(rows, remote_enabled=False)
        client = Client()
        response = client.get(f"/jobs/{job.pk}/{secret}/")
        assert response.status_code == 200

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
        response = client.get(f"/api/jobs/{job.pk}?secret=wrongsecret")
        # dev-open: auth gate passes, then per-job secret check gives 403
        assert response.status_code == 403


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
    COMPOSE_FILE = "/media/user/HDD/git/ClassyFire-wt-task11/docker-compose.yml"

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
