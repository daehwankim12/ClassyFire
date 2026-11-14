"""Settings contract tests.

Verifies that every Appendix A environment variable is either:
- parsed with a sensible default in base.py (safe for dev), or
- raises ImproperlyConfigured in production when missing.
"""
from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_prod_settings(monkeypatch: pytest.MonkeyPatch, env_overrides: dict[str, str]) -> types.ModuleType:
    """Import (or reimport) config.settings.prod with patched env vars.

    We build a minimal environment that satisfies prod's required checks,
    then selectively remove keys to trigger ImproperlyConfigured.
    """
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

    # Patch os.environ
    monkeypatch.setattr(os, "environ", {**os.environ, **base_env})

    # Remove cached modules so settings are re-evaluated
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("config.settings") or mod_name == "apps.core.settings":
            del sys.modules[mod_name]

    return importlib.import_module("config.settings.prod")


# ---------------------------------------------------------------------------
# Test: env helpers exist and are typed correctly
# ---------------------------------------------------------------------------

class TestEnvHelpers:
    def setup_method(self) -> None:
        # Ensure fresh module
        sys.modules.pop("apps.core.settings", None)

    def test_helpers_importable(self) -> None:
        from apps.core.settings import (  # noqa: F401
            env_bool,
            env_float,
            env_int,
            env_required,
            env_str,
        )

    def test_env_str_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("_TEST_MISSING_VAR", raising=False)
        from apps.core.settings import env_str
        assert env_str("_TEST_MISSING_VAR", "fallback") == "fallback"

    def test_env_str_reads_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("_TEST_STR_VAR", "hello")
        sys.modules.pop("apps.core.settings", None)
        from apps.core.settings import env_str
        assert env_str("_TEST_STR_VAR", "default") == "hello"

    def test_env_int_casts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("_TEST_INT_VAR", "42")
        from apps.core.settings import env_int
        assert env_int("_TEST_INT_VAR", 0) == 42
        assert isinstance(env_int("_TEST_INT_VAR", 0), int)

    def test_env_int_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("_TEST_INT_MISSING", raising=False)
        from apps.core.settings import env_int
        assert env_int("_TEST_INT_MISSING", 99) == 99

    def test_env_bool_true_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apps.core.settings import env_bool
        for val in ("true", "1", "yes", "True", "YES"):
            monkeypatch.setenv("_TEST_BOOL_VAR", val)
            assert env_bool("_TEST_BOOL_VAR", False) is True

    def test_env_bool_false_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apps.core.settings import env_bool
        for val in ("false", "0", "no", "False", "NO"):
            monkeypatch.setenv("_TEST_BOOL_VAR", val)
            assert env_bool("_TEST_BOOL_VAR", True) is False

    def test_env_bool_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("_TEST_BOOL_MISSING", raising=False)
        from apps.core.settings import env_bool
        assert env_bool("_TEST_BOOL_MISSING", True) is True

    def test_env_float_casts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("_TEST_FLOAT_VAR", "3.14")
        from apps.core.settings import env_float
        result = env_float("_TEST_FLOAT_VAR", 0.0)
        assert abs(result - 3.14) < 1e-9
        assert isinstance(result, float)

    def test_env_float_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("_TEST_FLOAT_MISSING", raising=False)
        from apps.core.settings import env_float
        assert env_float("_TEST_FLOAT_MISSING", 2.71) == pytest.approx(2.71)

    def test_env_required_raises_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("_TEST_REQUIRED_MISSING", raising=False)
        from apps.core.settings import env_required
        with pytest.raises(ImproperlyConfigured):
            env_required("_TEST_REQUIRED_MISSING")

    def test_env_required_returns_value_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("_TEST_REQUIRED_PRESENT", "my-value")
        from apps.core.settings import env_required
        assert env_required("_TEST_REQUIRED_PRESENT") == "my-value"

    def test_env_required_raises_on_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("_TEST_REQUIRED_EMPTY", "")
        from apps.core.settings import env_required
        with pytest.raises(ImproperlyConfigured):
            env_required("_TEST_REQUIRED_EMPTY")


# ---------------------------------------------------------------------------
# Test: base settings parse all Appendix A vars with defaults
# ---------------------------------------------------------------------------

class TestBaseSettingsContract:
    """Every Appendix A variable must be reachable in dev (no crash)."""

    @pytest.fixture(autouse=True)
    def _use_dev_settings(self, settings: object) -> None:
        # pytest-django already loaded dev settings; just confirm we can import base
        pass

    def test_wishart_base_url(self, settings: object) -> None:
        assert hasattr(settings, "WISHART_BASE_URL")
        assert str(settings.WISHART_BASE_URL).startswith(("http://", "https://"))

    def test_wishart_default_matches_live_http_endpoint(self) -> None:
        root = Path(__file__).resolve().parents[1]
        base_settings = (root / "config/settings/base.py").read_text()
        env_example = (root / ".env.example").read_text()

        assert '"WISHART_BASE_URL", "http://classyfire.wishartlab.com"' in base_settings
        assert "WISHART_BASE_URL=http://classyfire.wishartlab.com" in env_example

    def test_remote_enabled(self, settings: object) -> None:
        assert hasattr(settings, "REMOTE_ENABLED")
        assert isinstance(settings.REMOTE_ENABLED, bool)

    def test_remote_entity_requests_per_min(self, settings: object) -> None:
        assert hasattr(settings, "REMOTE_ENTITY_REQUESTS_PER_MIN")
        assert isinstance(settings.REMOTE_ENTITY_REQUESTS_PER_MIN, int)

    def test_remote_submits_per_min(self, settings: object) -> None:
        assert hasattr(settings, "REMOTE_SUBMITS_PER_MIN")
        assert isinstance(settings.REMOTE_SUBMITS_PER_MIN, int)

    def test_remote_batch_size(self, settings: object) -> None:
        assert hasattr(settings, "REMOTE_BATCH_SIZE")
        assert isinstance(settings.REMOTE_BATCH_SIZE, int)

    def test_remote_poll_min_seconds(self, settings: object) -> None:
        assert hasattr(settings, "REMOTE_POLL_MIN_SECONDS")
        assert isinstance(settings.REMOTE_POLL_MIN_SECONDS, int)

    def test_remote_max_concurrency(self, settings: object) -> None:
        assert hasattr(settings, "REMOTE_MAX_CONCURRENCY")
        assert isinstance(settings.REMOTE_MAX_CONCURRENCY, int)

    def test_remote_timeout_seconds(self, settings: object) -> None:
        assert hasattr(settings, "REMOTE_TIMEOUT_SECONDS")
        assert isinstance(settings.REMOTE_TIMEOUT_SECONDS, int)

    def test_max_upload_mb(self, settings: object) -> None:
        assert hasattr(settings, "MAX_UPLOAD_MB")
        assert isinstance(settings.MAX_UPLOAD_MB, int)
        assert settings.MAX_UPLOAD_MB > 0

    def test_upload_retention_days(self, settings: object) -> None:
        assert hasattr(settings, "UPLOAD_RETENTION_DAYS")
        assert isinstance(settings.UPLOAD_RETENTION_DAYS, int)

    def test_job_retention_days(self, settings: object) -> None:
        assert hasattr(settings, "JOB_RETENTION_DAYS")
        assert isinstance(settings.JOB_RETENTION_DAYS, int)
        assert settings.JOB_RETENTION_DAYS == 7

    def test_export_retention_days(self, settings: object) -> None:
        assert hasattr(settings, "EXPORT_RETENTION_DAYS")
        assert isinstance(settings.EXPORT_RETENTION_DAYS, int)
        assert settings.EXPORT_RETENTION_DAYS == 7

    def test_disk_upload_pause_percent(self, settings: object) -> None:
        assert hasattr(settings, "DISK_UPLOAD_PAUSE_PERCENT")
        assert isinstance(settings.DISK_UPLOAD_PAUSE_PERCENT, int)

    def test_g1_3_quota_settings_are_wired(self, settings: object) -> None:
        quota_settings = [
            "MAX_ROWS_PER_JOB",
            "JOB_CREATE_RATE_LIMIT_PER_MINUTE",
            "JOB_CREATE_DAILY_ROW_CAP",
            "ACTIVE_JOB_CAP",
            "EXPORT_CREATE_RATE_LIMIT_PER_MINUTE",
            "JOB_ACCESS_COOKIE_DAYS",
            "SHARED_ACCESS_LOGIN_FAILURE_LIMIT",
            "SHARED_ACCESS_LOGIN_FAILURE_WINDOW_SECONDS",
            "SHARED_ACCESS_LOGIN_MAX_FAILURES",
            "SHARED_ACCESS_LOGIN_WINDOW_SECONDS",
        ]
        for setting_name in quota_settings:
            assert hasattr(settings, setting_name)
            assert isinstance(getattr(settings, setting_name), int)

    def test_shared_access_password_hash_has_default(self, settings: object) -> None:
        # dev: empty string or any value is fine; just verify attribute exists
        assert hasattr(settings, "SHARED_ACCESS_PASSWORD_HASH")

    def test_ngrok_domain_has_default(self, settings: object) -> None:
        # dev: may be empty; attribute must exist
        assert hasattr(settings, "NGROK_DOMAIN")

    def test_ngrok_authtoken_has_default(self, settings: object) -> None:
        assert hasattr(settings, "NGROK_AUTHTOKEN")

    def test_database_settings_present(self, settings: object) -> None:
        db = settings.DATABASES["default"]
        assert db["ENGINE"] == "django.db.backends.postgresql"
        assert "HOST" in db
        assert "PORT" in db
        assert "NAME" in db
        assert "USER" in db


# ---------------------------------------------------------------------------
# Test: production required-var checks raise ImproperlyConfigured
# ---------------------------------------------------------------------------

class TestProdRequiredVars:
    """prod.py must raise ImproperlyConfigured if critical vars are absent."""

    def test_prod_loads_with_all_required_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _reload_prod_settings(monkeypatch, {})
        assert mod.DEBUG is False

    def test_prod_raises_without_secret_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ImproperlyConfigured):
            _reload_prod_settings(monkeypatch, {"DJANGO_SECRET_KEY": ""})

    @pytest.mark.parametrize(
        "secret_key",
        [
            "build-time-placeholder-not-for-use",
            "  build-time-placeholder-not-for-use  ",
        ],
    )
    def test_prod_rejects_known_secret_key_placeholder(
        self, monkeypatch: pytest.MonkeyPatch, secret_key: str
    ) -> None:
        with pytest.raises(ImproperlyConfigured, match="known unsafe placeholder"):
            _reload_prod_settings(monkeypatch, {"DJANGO_SECRET_KEY": secret_key})

    def test_prod_raises_without_postgres_password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ImproperlyConfigured):
            _reload_prod_settings(monkeypatch, {"POSTGRES_PASSWORD": ""})

    def test_prod_raises_without_ngrok_domain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ImproperlyConfigured):
            _reload_prod_settings(monkeypatch, {"NGROK_DOMAIN": ""})

    def test_prod_raises_without_shared_access_password_hash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ImproperlyConfigured):
            _reload_prod_settings(monkeypatch, {"SHARED_ACCESS_PASSWORD_HASH": ""})

    def test_prod_has_security_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _reload_prod_settings(monkeypatch, {})
        assert getattr(mod, "SECURE_SSL_REDIRECT", None) is True
        assert getattr(mod, "SESSION_COOKIE_SECURE", None) is True
        assert getattr(mod, "CSRF_COOKIE_SECURE", None) is True

    def test_prod_hsts_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _reload_prod_settings(monkeypatch, {})
        assert getattr(mod, "SECURE_HSTS_SECONDS", 0) > 0

    def test_prod_allowed_hosts_includes_ngrok_domain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _reload_prod_settings(monkeypatch, {"NGROK_DOMAIN": "myapp.ngrok-free.app"})
        assert "myapp.ngrok-free.app" in mod.ALLOWED_HOSTS

    def test_prod_csrf_trusted_origins_includes_ngrok_domain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _reload_prod_settings(monkeypatch, {"NGROK_DOMAIN": "myapp.ngrok-free.app"})
        origins = getattr(mod, "CSRF_TRUSTED_ORIGINS", [])
        assert any("myapp.ngrok-free.app" in o for o in origins)

    def test_prod_collectstatic_includes_project_static_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _reload_prod_settings(monkeypatch, {})
        project_static_dir = Path(mod.BASE_DIR) / "static"

        assert project_static_dir in getattr(mod, "STATICFILES_DIRS", [])
        assert (project_static_dir / "app.css").is_file()
