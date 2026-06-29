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
STATICFILES_DIRS = [BASE_DIR / "static"]

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
SHARED_ACCESS_LOGIN_FAILURE_LIMIT: int = env_int(
    "SHARED_ACCESS_LOGIN_FAILURE_LIMIT", 5
)
SHARED_ACCESS_LOGIN_FAILURE_WINDOW_SECONDS: int = env_int(
    "SHARED_ACCESS_LOGIN_FAILURE_WINDOW_SECONDS", 300
)
# DB-backed shared login throttle (apps.core.models.LoginThrottle). These are
# the canonical names the throttle reads; the legacy *_FAILURE_LIMIT /
# *_FAILURE_WINDOW_SECONDS names above are honoured as a fallback for backward
# compatibility (see apps.core.security._login_throttle_limit/_window).
SHARED_ACCESS_LOGIN_MAX_FAILURES: int = env_int(
    "SHARED_ACCESS_LOGIN_MAX_FAILURES", SHARED_ACCESS_LOGIN_FAILURE_LIMIT
)
SHARED_ACCESS_LOGIN_WINDOW_SECONDS: int = env_int(
    "SHARED_ACCESS_LOGIN_WINDOW_SECONDS", SHARED_ACCESS_LOGIN_FAILURE_WINDOW_SECONDS
)

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
    "WISHART_BASE_URL", "http://classyfire.wishartlab.com"
)
REMOTE_ENABLED: bool = env_bool("REMOTE_ENABLED", True)
# Entity/poll GETs are cheap cached reads (Wishart only caps the expensive POST
# submits at 12/min — confirmed: the official classyfire_api client applies NO
# hard GET limit, only a 0.2s/req pace ≈ 300/min). This is our SELF-IMPOSED,
# env-tunable politeness rate. 120/min keeps the entity-GET stage from being the
# throughput floor (the submit path tops out at 12 POST/min × 10 = 120/min) while
# staying well under the official client's ~300/min. The burst is pinned to 1 in
# DbRateLimiter.from_settings so this 2/sec budget is spent SMOOTHLY (no spike
# after an idle period) rather than as a 120-request thundering herd.
REMOTE_ENTITY_REQUESTS_PER_MIN: int = env_int("REMOTE_ENTITY_REQUESTS_PER_MIN", 120)
# Wishart caps POST (query submit) requests at 12/min — a fixed external limit,
# so this is hard-coded (NOT env-overridable) at a paced 10/min (one per ~6s).
# The submit token-bucket burst is pinned to 1 in DbRateLimiter.from_settings so
# a fresh worker cannot fire a whole minute's budget at once and trip the cap.
REMOTE_SUBMITS_PER_MIN: int = 10
# Structures per /queries POST. Wishart paginates completed-query JSON at 10
# compounds/page and our poll fetches a single page, so the effective batch is
# CLAMPED to that page size in worker._effective_batch_size — values above 10
# are silently capped (anything beyond page 1 would never be retrieved). 10 is
# the documented page size; 12 POST/min still bounds the submit rate.
REMOTE_BATCH_SIZE: int = env_int("REMOTE_BATCH_SIZE", 10)
REMOTE_POLL_MIN_SECONDS: int = env_int("REMOTE_POLL_MIN_SECONDS", 60)
REMOTE_MAX_CONCURRENCY: int = env_int("REMOTE_MAX_CONCURRENCY", 2)
REMOTE_TIMEOUT_SECONDS: int = env_int("REMOTE_TIMEOUT_SECONDS", 45)
WISHART_MAX_RESPONSE_BYTES: int = env_int("WISHART_MAX_RESPONSE_BYTES", 1_048_576)
# F-06: an in-doubt PENDING submission intent (crash after POST / before commit)
# is aged out to FAILED — freeing the (provider, payload_hash) idempotency key so
# exactly ONE genuine re-POST may occur — once EITHER it has been reconciled this
# many times OR it has existed for this many seconds, whichever comes first.
REMOTE_SUBMISSION_RECONCILE_MAX: int = env_int("REMOTE_SUBMISSION_RECONCILE_MAX", 3)
REMOTE_SUBMISSION_TTL_SECONDS: int = env_int(
    "REMOTE_SUBMISSION_TTL_SECONDS", 3600
)
# Give-up horizon for a stuck remote task. The queue NEVER discards a task on
# its own: once the retry schedule (1m→5m→30m→2h→6h→24h) is exhausted it parks
# as DEFERRED and retries ~daily forever, so a single permanently-unresolvable
# InChIKey keeps its BatchItem in REMOTE_DELAYED and pins the owning job to
# IN_PROGRESS indefinitely. After a task has been outstanding this many hours
# (measured from creation), the reaper marks it DONE and fans its items out to
# REMOTE_TIMED_OUT so the job can complete. Set to 0 to disable the reaper.
REMOTE_DELAYED_TIMEOUT_HOURS: int = env_int("REMOTE_DELAYED_TIMEOUT_HOURS", 24)
REMOTE_DAILY_BUDGET_PER_PRINCIPAL: int = env_int(
    "REMOTE_DAILY_BUDGET_PER_PRINCIPAL", 100
)
# H-05: global high-water cap on outstanding (active, non-DONE) remote tasks.
# Enforced race-safely in enqueue_task() under a select_for_update lock on the
# single RemoteQueueControl row, so N concurrent enqueuers across processes can
# never create more than this many active tasks. Kept high by default so normal
# batch ingest (which enqueues one task per unique InChIKey) is never throttled;
# the backpressure test overrides it to a small value to prove serialization.
REMOTE_MAX_QUEUE_DEPTH: int = env_int("REMOTE_MAX_QUEUE_DEPTH", 10000)

# ---------------------------------------------------------------------------
# Upload / retention
# ---------------------------------------------------------------------------

MAX_UPLOAD_MB: int = env_int("MAX_UPLOAD_MB", 100)
# Per-structure / per-record byte cap. Mirrors apps.core.security.MAX_STRUCTURE_LEN
# and bounds the upload physical-line / SDF-record scanner (M-02).
MAX_STRUCTURE_LEN: int = env_int("MAX_STRUCTURE_LEN", 100_000)
MEDIA_ROOT: str = env_str("MEDIA_ROOT", str(BASE_DIR / "media"))
UPLOAD_ARTIFACT_DIR: str = env_str("UPLOAD_ARTIFACT_DIR", str(BASE_DIR / "uploads"))
UPLOAD_RETENTION_DAYS: int = env_int("UPLOAD_RETENTION_DAYS", 30)
JOB_RETENTION_DAYS: int = env_int("JOB_RETENTION_DAYS", 90)
EXPORT_RETENTION_DAYS: int = env_int("EXPORT_RETENTION_DAYS", 14)
EXPORT_ARTIFACT_STORAGE_BACKEND: str = env_str("EXPORT_ARTIFACT_STORAGE_BACKEND", "local")
SOURCE_ARTIFACT_DIR: str = env_str("SOURCE_ARTIFACT_DIR", str(BASE_DIR / "source_artifacts"))
DISK_UPLOAD_PAUSE_PERCENT: int = env_int("DISK_UPLOAD_PAUSE_PERCENT", 85)
MAX_ROWS_PER_JOB: int = env_int("MAX_ROWS_PER_JOB", 10_000)
# F-11: when True (default) the upload routes STAGE BatchItems
# (PENDING_NORMALIZATION) and RETURN immediately with 202, leaving all
# normalization/lookup/upsert/task-create to the durable `process_jobs` worker.
# Set to False only in tests or diagnostics that need the synchronous in-request
# pipeline.
INGEST_DEFER_HEAVY_PROCESSING: bool = env_bool("INGEST_DEFER_HEAVY_PROCESSING", True)
# Ingest claim lease TTL. A claimed job is reclaimable by another worker only
# after its lease goes stale (claimed_at older than this many seconds), which
# lets a crashed worker's job resume while an actively-leased job is protected
# from theft. Every processing write is fenced to the claim owner+version.
INGEST_CLAIM_LEASE_SECONDS: int = env_int("INGEST_CLAIM_LEASE_SECONDS", 900)
JOB_CREATE_RATE_LIMIT_PER_MINUTE: int = env_int("JOB_CREATE_RATE_LIMIT_PER_MINUTE", 30)
JOB_CREATE_DAILY_ROW_CAP: int = env_int("JOB_CREATE_DAILY_ROW_CAP", 100_000)
ACTIVE_JOB_CAP: int = env_int("ACTIVE_JOB_CAP", 20)
EXPORT_CREATE_RATE_LIMIT_PER_MINUTE: int = env_int("EXPORT_CREATE_RATE_LIMIT_PER_MINUTE", 10)
JOB_ACCESS_COOKIE_DAYS: int = env_int("JOB_ACCESS_COOKIE_DAYS", 14)
# M-03: orphan export files (no ExportArtifact row) and stale temp partials
# from crashed streaming writes are deleted only when their mtime is older than
# this many seconds.  The grace period ensures an in-flight write is never
# raced.  Set via EXPORT_ORPHAN_GRACE_SECONDS env var; default 1 hour.
EXPORT_ORPHAN_GRACE_SECONDS: int = env_int("EXPORT_ORPHAN_GRACE_SECONDS", 3600)

# ---------------------------------------------------------------------------
# Referrer policy (defence-in-depth: URL path embeds a per-job secret)
# strict-origin trims the Referer to the origin only (scheme+host, no path), so
# the secret in a URL path never appears in any Referer header — while STILL
# sending a real Origin header. The latter matters behind the HTTPS proxy:
# "no-referrer" makes browsers send "Origin: null" on form POSTs, which fails
# Django's CSRF origin check and 403s every POST. SecurityMiddleware emits
# Referrer-Policy when this setting is present.
# ---------------------------------------------------------------------------

SECURE_REFERRER_POLICY = "strict-origin"

# ---------------------------------------------------------------------------
# Circuit breaker (Task 9 remote worker — read via getattr for backwards compat)
# ---------------------------------------------------------------------------

REMOTE_CIRCUIT_THRESHOLD: int = env_int("REMOTE_CIRCUIT_THRESHOLD", 5)
REMOTE_CIRCUIT_RESET_SECONDS: int = env_int("REMOTE_CIRCUIT_RESET_SECONDS", 300)

AUDIT_SIGNING_KEY: str = env_str("AUDIT_SIGNING_KEY", "")
