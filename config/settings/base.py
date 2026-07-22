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
# Display timezone. USE_TZ=True keeps ALL stored datetimes in UTC in the DB; this
# only controls how aware datetimes are rendered (templates, timezone.localtime).
# Override via TIME_ZONE env if deploying in another region.
TIME_ZONE = env_str("TIME_ZONE", "Asia/Seoul")
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
# ClassyFire remote API
# ---------------------------------------------------------------------------

WISHART_BASE_URL: str = env_str(
    "WISHART_BASE_URL", "http://classyfire.wishartlab.com"
)
REMOTE_ENABLED: bool = env_bool("REMOTE_ENABLED", True)
# Wishart rate-limits per IP, pinned to observed live-server 429 behaviour.
# These endpoint-specific ceilings are hard-coded (NOT env-overridable), but the
# production DbRateLimiter consumes the stricter value from ONE shared
# "request" bucket for entity GETs, polls, and POST /queries. The live service
# 429s on aggregate traffic, so separate GET/POST buckets can overrun the IP cap.
# Burst is pinned to 1 so a fresh worker spends the budget smoothly instead of
# bursting a whole minute at once and tripping the cap into Retry-After parks.
REMOTE_ENTITY_REQUESTS_PER_MIN: int = 10
REMOTE_SUBMITS_PER_MIN: int = 12
# Structures per /queries POST. Default 1 — each entity MISS is POSTed on its
# own, never bundling un-checked extra tasks. This keeps known compounds resolving
# via the cheap entity GET instead of being dragged into the slower POST path by
# an interspersed new compound. (When >1, the effective batch is still clamped to
# Wishart's 10-compounds/page JSON limit in worker._effective_batch_size — values
# above 10 would strand entities past page 1.)
REMOTE_BATCH_SIZE: int = env_int("REMOTE_BATCH_SIZE", 1)
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
# Structure resolution: when an InChIKey-only item misses local caches AND
# Wishart's entity endpoint (NEED_STRUCTURE), resolve its structure from public
# key-free DBs, verify it re-hashes to the SAME InChIKey, and auto-submit it for
# calculation. Strictly additive — an unresolved key stays NEED_STRUCTURE. The
# ordered provider chain is key-free; per-provider polite rates (spent smoothly,
# burst 1) are hard-coded in apps/remote/structure_providers.py.
STRUCTURE_RESOLVE_ENABLED: bool = env_bool("STRUCTURE_RESOLVE_ENABLED", True)
STRUCTURE_RESOLVE_PROVIDERS: tuple[str, ...] = tuple(
    p.strip().lower()
    for p in env_str("STRUCTURE_RESOLVE_PROVIDERS", "pubchem,cactus").split(",")
    if p.strip()
)
STRUCTURE_RESOLVE_TIMEOUT_SECONDS: int = env_int("STRUCTURE_RESOLVE_TIMEOUT_SECONDS", 15)
# Max distinct InChIKeys SCANNED per background sweep pass. Outbound call volume
# is bounded by the per-provider rate limiter (not this); cached-miss keys are
# skipped cheaply, so this just needs to comfortably exceed the count of
# still-pending NEED_STRUCTURE keys so the tail is never starved.
STRUCTURE_RESOLVE_SWEEP_LIMIT: int = env_int("STRUCTURE_RESOLVE_SWEEP_LIMIT", 200)
# How long a DEFINITIVE all-provider miss (compound in no public DB) is cached so
# it is not re-queried every sweep. Transient skips are never cached. 0 disables.
STRUCTURE_RESOLVE_MISS_TTL_SECONDS: int = env_int(
    "STRUCTURE_RESOLVE_MISS_TTL_SECONDS", 21600
)
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
JOB_RETENTION_DAYS: int = env_int("JOB_RETENTION_DAYS", 7)
EXPORT_RETENTION_DAYS: int = env_int("EXPORT_RETENTION_DAYS", 7)
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
# How long a job parked on queue BACKPRESSURE waits before a worker may
# re-claim it and re-probe its REMOTE_DELAYED rows. Deliberately SEPARATE from
# the claim lease above: the lease is crash detection, this is the retry
# cadence, and tying the two would make raising the lease for big jobs silently
# slow retries down. A spent DAILY budget uses a different gate (the next budget
# window start) rather than this interval, since it does not refill until then.
# Floored to >= 1s at use time so a zero/negative value cannot hot-loop the
# worker.
REMOTE_DELAY_RETRY_SECONDS: int = env_int("REMOTE_DELAY_RETRY_SECONDS", 60)
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
