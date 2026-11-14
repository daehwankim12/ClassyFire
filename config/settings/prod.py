"""Production settings.

Security-hardened configuration for the live deployment.  This module must
never be imported in development; dev.py is the entry-point for local work.

Required environment variables (raises ImproperlyConfigured when absent):
- DJANGO_SECRET_KEY
- POSTGRES_PASSWORD
- NGROK_DOMAIN
- SHARED_ACCESS_PASSWORD_HASH
"""
from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured

from apps.core.settings import env_required, env_str

# Import all base settings first
from .base import *  # noqa: F403

# ---------------------------------------------------------------------------
# Required secrets — raise early with a clear message if any are absent
# ---------------------------------------------------------------------------

_UNSAFE_SECRET_KEYS = frozenset({"build-time-placeholder-not-for-use"})

SECRET_KEY: str = env_required("DJANGO_SECRET_KEY")  # type: ignore[no-redef]
if SECRET_KEY.strip() in _UNSAFE_SECRET_KEYS:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY uses a known unsafe placeholder; configure a unique secret."
    )

_postgres_password: str = env_required("POSTGRES_PASSWORD")
DATABASES["default"]["PASSWORD"] = _postgres_password  # noqa: F405 (from base *)

NGROK_DOMAIN: str = env_required("NGROK_DOMAIN")  # type: ignore[no-redef]

SHARED_ACCESS_PASSWORD_HASH: str = env_required("SHARED_ACCESS_PASSWORD_HASH")  # type: ignore[no-redef]

# ---------------------------------------------------------------------------
# Core security
# ---------------------------------------------------------------------------

DEBUG = False

ALLOWED_HOSTS: list[str] = [NGROK_DOMAIN, f"www.{NGROK_DOMAIN}"]  # type: ignore[no-redef]

CSRF_TRUSTED_ORIGINS: list[str] = [
    f"https://{NGROK_DOMAIN}",
    f"https://www.{NGROK_DOMAIN}",
]

# ---------------------------------------------------------------------------
# HTTPS / cookie hardening
# ---------------------------------------------------------------------------

# Redirect all HTTP → HTTPS
SECURE_SSL_REDIRECT = True

# Tell Django the connection is HTTPS even when Caddy/ngrok terminates TLS
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# HSTS: 1 year, include subdomains
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# Prevent browsers from guessing content types
SECURE_CONTENT_TYPE_NOSNIFF = True

# Session and CSRF cookies
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE: str = "Lax"

CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE: str = "Lax"

# Clickjacking
X_FRAME_OPTIONS = "DENY"

# ---------------------------------------------------------------------------
# Static files (whitenoise handles compression + cache headers in prod)
# ---------------------------------------------------------------------------

STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
}

# ---------------------------------------------------------------------------
# Export artifact storage (Gate-2 WS-E / F-12)
#
# In production the export_files named Docker volume is mounted at
# /app/export_files so artifacts survive container recreation.
# EXPORT_DIR is read by apps/batches/exports.py; the dev default is
# BASE_DIR/export_cache which remains unchanged for local development.
# ---------------------------------------------------------------------------

from apps.core.settings import env_str as _env_str  # noqa: E402

EXPORT_DIR = _env_str("EXPORT_DIR", "/app/export_files")
SOURCE_ARTIFACT_DIR = _env_str("SOURCE_ARTIFACT_DIR", "/app/source_artifacts")
MEDIA_ROOT = _env_str("MEDIA_ROOT", "/app/media")
UPLOAD_ARTIFACT_DIR = _env_str("UPLOAD_ARTIFACT_DIR", "/app/uploads")

# ---------------------------------------------------------------------------
# Logging (minimal — full observability is Task 11)
# ---------------------------------------------------------------------------

_DJANGO_LOG_LEVEL = env_str("DJANGO_LOG_LEVEL", "WARNING")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "redact_secrets": {
            "()": "apps.core.security.SecretRedactionFilter",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["redact_secrets"],
        },
    },
    "root": {
        "handlers": ["console"],
        "level": _DJANGO_LOG_LEVEL,
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": _DJANGO_LOG_LEVEL,
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": _DJANGO_LOG_LEVEL,
            "propagate": False,
        },
        "django.security": {
            "handlers": ["console"],
            "level": _DJANGO_LOG_LEVEL,
            "propagate": False,
        },
        "django.server": {
            "handlers": ["console"],
            "level": _DJANGO_LOG_LEVEL,
            "propagate": False,
        },
    },
}

# ---------------------------------------------------------------------------
# H-05: Remote budget guard — a non-positive budget in production is a
# misconfiguration that must fail loudly at startup.
# ---------------------------------------------------------------------------

if REMOTE_DAILY_BUDGET_PER_PRINCIPAL <= 0:  # noqa: F405
    raise ImproperlyConfigured(
        "A positive production remote budget is required"
        " (REMOTE_DAILY_BUDGET_PER_PRINCIPAL must be > 0 in production)"
    )
