"""Base settings shared across all environments."""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

from apps.core.settings import env_bool, env_int, env_str

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Load .env for local development convenience (no-op if file is absent).
load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

# In base.py we read the key with a fallback of "" so that importing the
# module never crashes.  prod.py calls env_required() which raises when it is
# absent or empty.
SECRET_KEY: str = env_str("DJANGO_SECRET_KEY", "")

ALLOWED_HOSTS: list[str] = []

DEBUG = False  # overridden per environment

# ---------------------------------------------------------------------------
# Application definition
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_htmx",
    "apps.core",
    "apps.chemont",
    "apps.classifications",
    "apps.compounds",
    "apps.batches",
    "apps.remote",
    "apps.operations",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": env_str("POSTGRES_HOST", "db"),
        "PORT": env_str("POSTGRES_PORT", "5432"),
        "NAME": env_str("POSTGRES_DB", "classyfire"),
        "USER": env_str("POSTGRES_USER", "classyfire"),
        "PASSWORD": env_str("POSTGRES_PASSWORD", ""),
    }
}

# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# Internationalisation
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Ngrok / tunnel
# ---------------------------------------------------------------------------

NGROK_AUTHTOKEN: str = env_str("NGROK_AUTHTOKEN", "")
NGROK_DOMAIN: str = env_str("NGROK_DOMAIN", "")

# ---------------------------------------------------------------------------
# Shared-access gate
# ---------------------------------------------------------------------------

SHARED_ACCESS_PASSWORD_HASH: str = env_str("SHARED_ACCESS_PASSWORD_HASH", "")

# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

SEED_PROFILE: str = env_str("SEED_PROFILE", "FULL")
SEED_RECORD_ID: int = env_int("SEED_RECORD_ID", 20472700)
SEED_AUTO_UPDATE: bool = env_bool("SEED_AUTO_UPDATE", False)

# ---------------------------------------------------------------------------
# ClassyFire remote API
# ---------------------------------------------------------------------------

WISHART_BASE_URL: str = env_str(
    "WISHART_BASE_URL", "https://classyfire.wishartlab.com"
)
REMOTE_ENABLED: bool = env_bool("REMOTE_ENABLED", True)
REMOTE_ENTITY_REQUESTS_PER_MIN: int = env_int("REMOTE_ENTITY_REQUESTS_PER_MIN", 10)
REMOTE_SUBMITS_PER_MIN: int = env_int("REMOTE_SUBMITS_PER_MIN", 1)
REMOTE_BATCH_SIZE: int = env_int("REMOTE_BATCH_SIZE", 25)
REMOTE_POLL_MIN_SECONDS: int = env_int("REMOTE_POLL_MIN_SECONDS", 60)
REMOTE_MAX_CONCURRENCY: int = env_int("REMOTE_MAX_CONCURRENCY", 2)
REMOTE_TIMEOUT_SECONDS: int = env_int("REMOTE_TIMEOUT_SECONDS", 45)

# ---------------------------------------------------------------------------
# Upload / retention
# ---------------------------------------------------------------------------

MAX_UPLOAD_MB: int = env_int("MAX_UPLOAD_MB", 100)
UPLOAD_RETENTION_DAYS: int = env_int("UPLOAD_RETENTION_DAYS", 30)
JOB_RETENTION_DAYS: int = env_int("JOB_RETENTION_DAYS", 90)
EXPORT_RETENTION_DAYS: int = env_int("EXPORT_RETENTION_DAYS", 14)
DISK_UPLOAD_PAUSE_PERCENT: int = env_int("DISK_UPLOAD_PAUSE_PERCENT", 85)

# ---------------------------------------------------------------------------
# Circuit breaker (Task 9 remote worker — read via getattr for backwards compat)
# ---------------------------------------------------------------------------

REMOTE_CIRCUIT_THRESHOLD: int = env_int("REMOTE_CIRCUIT_THRESHOLD", 5)
REMOTE_CIRCUIT_RESET_SECONDS: int = env_int("REMOTE_CIRCUIT_RESET_SECONDS", 300)
