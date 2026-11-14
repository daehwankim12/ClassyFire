"""Security helpers for ClassyFire Batch Cache Web.

Provides:
- require_shared_access: decorator gating API views behind shared-password auth.
- upload safety helpers: size, row-length, SDF-property, decompression-ratio guards.
- redact_for_log: ensures secrets and structures are never emitted in log output.

Auth design
-----------
The gate checks, in order:
  1. Authorization: Bearer <token> header (JSON API, CSRF not applicable)
  2. Session login (Django staff/superuser)
  3. X-Shared-Access-Token header (alternative header form)

When SHARED_ACCESS_PASSWORD_HASH is set in settings, the bearer token is
validated against it via constant-time check_password.  When it is NOT set
(dev-open mode), the gate passes all requests — this allows existing tests and
local development to function without configuration.

CSRF note: API POST endpoints preserve bearer-token ergonomics, but ambient
browser credentials (shared sessions or job-access cookies) must pass Django
CSRF checks.

PROD mode: if ACCESS_MODE="strict" or SHARED_ACCESS_PASSWORD_HASH is non-empty
in prod.py (enforced via env_required), the gate will deny all unauthenticated
requests.
"""
from __future__ import annotations

import functools
import hashlib
import io
import logging
import re
import shutil
import zipfile
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.core import signing
from django.db import connection, transaction
from django.db.models import Sum
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import CsrfViewMiddleware
from django.utils import timezone

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from apps.batches.models import BatchJob

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_STRUCTURE_LEN: int = 100_000  # bytes per structure string
MAX_SDF_PROPERTY_LEN: int = 10_000  # bytes per SDF property value
MAX_DECOMPRESSION_RATIO: float = 100.0  # reject if expanded > N x compressed

SHARED_ACCESS_SESSION_KEY = "shared_access_authenticated"
SHARED_ACCESS_PRINCIPAL_SESSION_KEY = "shared_access_principal"
JOB_ACCESS_COOKIE_PREFIX = "cf_job_access_"
JOB_ACCESS_COOKIE_SALT = "classyfire-job-access"


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def _get_shared_hash() -> str:
    return getattr(settings, "SHARED_ACCESS_PASSWORD_HASH", "") or ""


def _is_dev_open() -> bool:
    """True when no password hash is configured — dev/test open mode."""
    return not _get_shared_hash().strip()


def _shared_access_token_from_request(request: HttpRequest) -> tuple[str, str] | None:
    """Return the presented shared-access token kind and value, if any."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return "bearer", auth_header[len("Bearer "):].strip()
    token = request.headers.get("X-Shared-Access-Token", "").strip()
    if token:
        return "shared-token", token
    return None


def _shared_access_principal_for_token(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
    return f"shared-access:{digest}"


def _check_bearer(request: HttpRequest) -> bool:
    """Validate bearer token from Authorization header or X-Shared-Access-Token."""
    token_info = _shared_access_token_from_request(request)
    token = token_info[1] if token_info is not None else ""

    if not token:
        return False

    stored_hash = _get_shared_hash()
    if not stored_hash:
        return False

    return bool(check_password(token, stored_hash))


def _is_authenticated(request: HttpRequest, job_id: str | None = None) -> bool:
    """Return True if the request carries valid credentials."""
    if _is_dev_open():
        return True

    if job_id is not None and request_has_job_access_cookie(request, job_id):
        return True

    if _check_bearer(request):
        return True

    if request.session.get(SHARED_ACCESS_SESSION_KEY) is True:
        return True

    if hasattr(request, "user") and request.user.is_authenticated:
        return True

    return False


def require_shared_access(view_func: Callable) -> Callable:
    """Decorator: gate a view behind shared-access authentication.

    In dev-open mode (SHARED_ACCESS_PASSWORD_HASH unset), all requests pass.
    In secure mode, a valid Bearer token or session login is required.
    Returns a JSON 401 response for unauthenticated requests (suitable for
    API endpoints).  Use :func:`require_shared_access_web` for browser views.
    """
    @functools.wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs):
        if not _is_authenticated(request, kwargs.get("job_id")):
            return JsonResponse(
                {"error": "Authentication required."},
                status=401,
                headers={"WWW-Authenticate": 'Bearer realm="ClassyFire"'},
            )
        return view_func(request, *args, **kwargs)

    return wrapper


def require_csrf_for_cookie_auth(view_func: Callable) -> Callable:
    """Require CSRF only for unsafe requests using ambient browser credentials."""
    @functools.wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs):
        if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
            return view_func(request, *args, **kwargs)
        if _is_dev_open() or _check_bearer(request):
            return view_func(request, *args, **kwargs)

        csrf = CsrfViewMiddleware(lambda _request: HttpResponse())
        rejection = csrf.process_view(request, lambda _request: HttpResponse(), (), {})
        if rejection is not None:
            return rejection
        return view_func(request, *args, **kwargs)

    return wrapper


def require_shared_access_web(view_func: Callable) -> Callable:
    """Decorator: gate a browser view behind shared-access authentication.

    Identical auth logic to :func:`require_shared_access` but returns a
    browser-friendly 403 Forbidden response instead of a JSON 401.  This
    keeps the gate DRY — both decorators delegate to :func:`_is_authenticated`
    — while giving browsers a useful HTTP status code rather than a JSON body.

    In dev-open mode (SHARED_ACCESS_PASSWORD_HASH unset), all requests pass.
    In secure mode, a valid Bearer token, session login, or
    X-Shared-Access-Token header is required; unauthenticated browser requests
    are redirected to the shared-access login form.
    """
    from django.shortcuts import redirect  # local import to avoid circular
    from django.urls import reverse

    @functools.wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs):
        if not _is_authenticated(request, kwargs.get("job_id")):
            access_url = reverse("shared_access")
            return redirect(f"{access_url}?next={request.get_full_path()}")
        return view_func(request, *args, **kwargs)

    return wrapper


def authenticate_shared_password(request: HttpRequest, password: str) -> bool:
    """Persist a browser shared-access session when *password* matches."""
    stored_hash = _get_shared_hash()
    if not stored_hash or not check_password(password, stored_hash):
        return False
    if request.session.session_key is None:
        request.session.create()
    else:
        request.session.cycle_key()
    request.session[SHARED_ACCESS_SESSION_KEY] = True
    request.session[SHARED_ACCESS_PRINCIPAL_SESSION_KEY] = (
        _shared_access_principal_for_token(password)
    )
    clear_shared_access_login_failures(request)
    return True


def request_has_shared_access(request: HttpRequest, job_id: object | None = None) -> bool:
    """Return True when request carries app-level or job-cookie credentials."""
    return _is_authenticated(request, str(job_id) if job_id is not None else None)


def throttle_key_for_ip(ip_address: str) -> str:
    """Return the stable, DB-stored throttle key for a client IP address.

    The raw IP is hashed so the ``login_throttles`` table never stores client
    addresses in the clear.
    """
    ip_digest = hashlib.sha256(ip_address.encode("utf-8")).hexdigest()
    return f"classyfire:shared-access-login-failures:{ip_digest}"


def _shared_access_login_failure_key(request: HttpRequest) -> str:
    return throttle_key_for_ip(_client_ip(request))


def _login_throttle_limit() -> int:
    """Resolve the shared-access login failure cap.

    Prefers the canonical ``SHARED_ACCESS_LOGIN_MAX_FAILURES`` setting and falls
    back to the legacy ``SHARED_ACCESS_LOGIN_FAILURE_LIMIT`` name so existing
    deployments and tests that set only the legacy name keep working.
    """
    if hasattr(settings, "SHARED_ACCESS_LOGIN_MAX_FAILURES"):
        return int(settings.SHARED_ACCESS_LOGIN_MAX_FAILURES)
    return int(getattr(settings, "SHARED_ACCESS_LOGIN_FAILURE_LIMIT", 5))


def _login_throttle_window_seconds() -> int:
    if hasattr(settings, "SHARED_ACCESS_LOGIN_WINDOW_SECONDS"):
        return int(settings.SHARED_ACCESS_LOGIN_WINDOW_SECONDS)
    return int(getattr(settings, "SHARED_ACCESS_LOGIN_FAILURE_WINDOW_SECONDS", 300))


def record_failed_login_attempt(ip_address: str) -> bool:
    """Atomically record one failed login for *ip_address* (DB-backed).

    Returns ``True`` iff THIS call is the one that CROSSES the failure threshold
    (i.e. moved the shared counter from below the cap to exactly the cap). This
    "crossing" semantics is what makes the throttle's atomicity observable: under
    concurrency, ``select_for_update`` serialises the increments so that across N
    racing callers exactly one observes the crossing (returns True). Callers that
    only need to know whether the IP is *currently* blocked must use
    :func:`is_shared_access_login_throttled` / :func:`_ip_is_login_throttled`,
    which read ``failure_count >= limit``.
    """
    from django.db import IntegrityError

    from apps.core.models import LoginThrottle

    limit = _login_throttle_limit()
    if limit < 0:
        return False

    key = throttle_key_for_ip(ip_address)
    now = timezone.now()
    window = timedelta(seconds=_login_throttle_window_seconds())

    # Phase 1: ensure the row EXISTS and is committed, in its own savepoint.
    # Doing the create separately means a unique-constraint race between racing
    # workers raises IntegrityError HERE (rolled back to this savepoint only) and
    # never poisons the lock+increment transaction below. Without this split the
    # losing INSERTs of a concurrent get_or_create abort the whole transaction in
    # PostgreSQL, silently dropping those workers' increments (lost updates).
    try:
        with transaction.atomic():
            LoginThrottle.objects.create(
                key=key, failure_count=0, window_started_at=now
            )
    except IntegrityError:
        pass  # Row already created by a concurrent worker — fine.

    # Phase 2: lock the now-guaranteed row and increment atomically. The
    # select_for_update serialises concurrent workers so increments cannot be
    # lost and exactly one worker observes the threshold-crossing.
    with transaction.atomic():
        row = LoginThrottle.objects.select_for_update().get(key=key)
        if row.window_started_at <= now - window:
            row.failure_count = 0
            row.window_started_at = now
        row.failure_count += 1
        row.save(update_fields=["failure_count", "window_started_at", "updated_at"])
        # Crossing semantics: True only on the call that hits the cap exactly.
        return row.failure_count == limit


def _ip_is_login_throttled(ip_address: str) -> bool:
    """Return True when *ip_address* is currently blocked (DB-backed gate read)."""
    from apps.core.models import LoginThrottle

    limit = _login_throttle_limit()
    if limit < 0:
        return False

    key = throttle_key_for_ip(ip_address)
    now = timezone.now()
    window = timedelta(seconds=_login_throttle_window_seconds())
    row = LoginThrottle.objects.filter(key=key).only(
        "failure_count", "window_started_at"
    ).first()
    if row is None:
        return False
    if row.window_started_at <= now - window:
        # The window has fully elapsed: the recorded failures no longer count.
        return False
    return row.failure_count >= limit


def is_shared_access_login_throttled(request: HttpRequest) -> bool:
    """Return True when repeated failed shared-password attempts hit the cap."""
    return _ip_is_login_throttled(_client_ip(request))


def record_shared_access_login_failure(request: HttpRequest) -> None:
    record_failed_login_attempt(_client_ip(request))


def clear_shared_access_login_failures(request: HttpRequest) -> None:
    from apps.core.models import LoginThrottle

    LoginThrottle.objects.filter(key=_shared_access_login_failure_key(request)).delete()


# ---------------------------------------------------------------------------
# Per-job access cookies
# ---------------------------------------------------------------------------


def job_access_cookie_name(job_id: object) -> str:
    normalized = str(job_id).replace("-", "")
    return f"{JOB_ACCESS_COOKIE_PREFIX}{normalized}"


def _job_cookie_max_age_seconds() -> int:
    days = int(getattr(settings, "JOB_ACCESS_COOKIE_DAYS", 14))
    return max(1, days) * 24 * 60 * 60


def _job_access_signer() -> signing.TimestampSigner:
    return signing.TimestampSigner(salt=JOB_ACCESS_COOKIE_SALT)


def set_job_access_cookie(response: HttpResponse, request: HttpRequest, job_id: object) -> None:
    """Set an HttpOnly SameSite=Lax cookie proving access to one job."""
    secure = bool(getattr(settings, "SESSION_COOKIE_SECURE", False) or request.is_secure())
    response.set_cookie(
        job_access_cookie_name(job_id),
        _job_access_signer().sign(str(job_id)),
        max_age=_job_cookie_max_age_seconds(),
        httponly=True,
        secure=secure,
        samesite="Lax",
    )


def request_has_job_access_cookie(request: HttpRequest, job_id: object) -> bool:
    value = request.COOKIES.get(job_access_cookie_name(job_id), "")
    if not value:
        return False
    try:
        unsigned = _job_access_signer().unsign(
            value,
            max_age=_job_cookie_max_age_seconds(),
        )
    except signing.BadSignature:
        return False
    return unsigned == str(job_id)


# ---------------------------------------------------------------------------
# Job creation and export quotas
# ---------------------------------------------------------------------------


def _client_ip(request: HttpRequest) -> str:
    forwarded_for = str(request.META.get("HTTP_X_FORWARDED_FOR", ""))
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return str(request.META.get("REMOTE_ADDR", "") or "0.0.0.0")


def _principal(request: HttpRequest) -> str:
    if hasattr(request, "user") and request.user.is_authenticated:
        return f"user:{request.user.pk}"
    token_info = _shared_access_token_from_request(request)
    if token_info is not None and _check_bearer(request):
        return _shared_access_principal_for_token(token_info[1])
    if hasattr(request, "session") and request.session.get(SHARED_ACCESS_SESSION_KEY) is True:
        principal = request.session.get(SHARED_ACCESS_PRINCIPAL_SESSION_KEY)
        if isinstance(principal, str) and principal.startswith("shared-access:"):
            return principal
        digest = hashlib.sha256(_get_shared_hash().encode("utf-8")).hexdigest()[:32]
        return f"shared-access-session:{digest}"
    ip_digest = hashlib.sha256(_client_ip(request).encode("utf-8")).hexdigest()[:32]
    return f"ip:{ip_digest}"


def _quota_response(message: str, status: int = 429) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def quota_advisory_lock_keys(kind: str, principal: str, ip_address: str) -> tuple[int, int]:
    """Return stable signed 32-bit keys for PostgreSQL advisory locks."""
    digest = hashlib.sha256(f"{kind}\0{principal}\0{ip_address}".encode()).digest()
    return (
        int.from_bytes(digest[:4], "big", signed=True),
        int.from_bytes(digest[4:8], "big", signed=True),
    )


def _acquire_quota_advisory_lock(kind: str, principal: str, ip_address: str) -> None:
    key_1, key_2 = quota_advisory_lock_keys(kind, principal, ip_address)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [key_1, key_2])


def _acquire_quota_advisory_locks(kind: str, principal: str, ip_address: str) -> None:
    _acquire_quota_advisory_lock(kind, f"principal:{principal}", "")
    _acquire_quota_advisory_lock(kind, "", f"ip:{ip_address}")
    _acquire_quota_advisory_lock(kind, "global", "active")


def _disk_usage_percent(path: Path) -> float:
    usage = shutil.disk_usage(path)
    if usage.total <= 0:
        return 0.0
    return ((usage.total - usage.free) / usage.total) * 100


def _upload_root() -> Path:
    from apps.batches.ingest import validate_upload_root

    root = validate_upload_root(getattr(settings, "UPLOAD_ARTIFACT_DIR", ""))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _retained_upload_bytes_on_disk(root: Path) -> int:
    """Return bytes currently retained in the configured upload directory."""
    total = 0
    for artifact in root.iterdir():
        try:
            if artifact.is_file():
                total += artifact.stat().st_size
        except FileNotFoundError:
            # Retention cleanup may remove an artifact during this read. That
            # can only increase the available allowance, so retry is unnecessary.
            continue
    return total


def _principal_retained_upload_bytes(principal: str, max_upload_bytes: int) -> int:
    """Return reserved bytes for one principal's still-linked raw artifacts."""
    from apps.batches.models import BatchJob

    sizes = (
        BatchJob.objects.filter(remote_principal=principal)
        .exclude(upload_artifact_path__isnull=True)
        .exclude(upload_artifact_path="")
        .values_list("upload_size_bytes", flat=True)
    )
    # Legacy/manual rows may lack size metadata. Charge them at the maximum
    # upload size rather than letting unknown retained files bypass the quota.
    return sum(
        int(size) if size is not None and int(size) >= 0 else max_upload_bytes
        for size in sizes
    )


def _retained_upload_allowance(
    principal: str,
) -> tuple[int, JsonResponse | None]:
    """Return the maximum bytes one new spool may retain under all byte caps.

    The caller holds the principal and global job-create advisory locks while
    obtaining this allowance, streaming the upload, and inserting its BatchJob
    reservation. Therefore concurrent admissions cannot spend the same bytes.
    """
    root = _upload_root()
    max_upload_bytes = int(getattr(settings, "MAX_UPLOAD_MB", 100)) * 1024 * 1024
    principal_cap = int(
        getattr(settings, "UPLOAD_RETAINED_BYTES_PER_PRINCIPAL", 500 * 1024 * 1024)
    )
    global_cap = int(
        getattr(settings, "UPLOAD_RETAINED_BYTES_GLOBAL", 2 * 1024 * 1024 * 1024)
    )

    principal_used = _principal_retained_upload_bytes(principal, max_upload_bytes)
    global_used = _retained_upload_bytes_on_disk(root)

    usage = shutil.disk_usage(root)
    pause_percent = int(getattr(settings, "DISK_UPLOAD_PAUSE_PERCENT", 85))
    pause_bytes = int(usage.total * pause_percent / 100)
    disk_used = usage.total - usage.free

    candidates: list[tuple[int, JsonResponse | None]] = [
        (max(max_upload_bytes, 0), None),
        (
            max(principal_cap - principal_used, 0),
            _quota_response("Retained upload byte quota exceeded."),
        ),
        (
            max(global_cap - global_used, 0),
            _quota_response("Global retained upload byte quota exceeded.", status=503),
        ),
        (
            max(pause_bytes - disk_used, 0),
            _quota_response(
                "New job creation is paused because upload disk usage is above "
                "the configured limit.",
                status=503,
            ),
        ),
    ]
    return min(candidates, key=lambda candidate: candidate[0])


def validate_job_create_controls(request: HttpRequest, row_count: int) -> JsonResponse | None:
    """Return a rejection response when job creation would exceed limits."""
    pause_percent = int(getattr(settings, "DISK_UPLOAD_PAUSE_PERCENT", 85))
    if _disk_usage_percent(_upload_root()) >= pause_percent:
        return _quota_response(
            "New job creation is paused because upload disk usage is above the "
            "configured limit.",
            status=503,
        )

    max_rows = int(getattr(settings, "MAX_ROWS_PER_JOB", 10_000))
    if row_count > max_rows:
        return _quota_response(
            f"Job has {row_count} rows; the maximum is {max_rows}.",
            status=413,
        )

    from apps.batches.models import BatchJob, BatchQuotaEvent
    from apps.core.enums import BatchJobStatus

    now = timezone.now()
    minute_ago = now - timedelta(minutes=1)
    day_ago = now - timedelta(days=1)
    principal = _principal(request)
    ip_address = _client_ip(request)

    per_minute = int(getattr(settings, "JOB_CREATE_RATE_LIMIT_PER_MINUTE", 30))
    if per_minute >= 0:
        recent = BatchQuotaEvent.objects.filter(
            kind=BatchQuotaEvent.KIND_JOB_CREATE,
            created_at__gte=minute_ago,
        ).filter(principal=principal)
        recent_by_ip = BatchQuotaEvent.objects.filter(
            kind=BatchQuotaEvent.KIND_JOB_CREATE,
            created_at__gte=minute_ago,
            ip_address=ip_address,
        )
        if recent.count() >= per_minute or recent_by_ip.count() >= per_minute:
            return _quota_response("Job creation rate limit exceeded.")

    daily_cap = int(getattr(settings, "JOB_CREATE_DAILY_ROW_CAP", 100_000))
    used_rows = (
        BatchQuotaEvent.objects.filter(
            kind__in=[
                BatchQuotaEvent.KIND_JOB_CREATE,
                BatchQuotaEvent.KIND_JOB_ROWS,
            ],
            created_at__gte=day_ago,
        )
        .filter(principal=principal)
        .aggregate(total=Sum("row_count"))["total"]
        or 0
    )
    if used_rows + row_count > daily_cap:
        return _quota_response("Daily row quota exceeded.")

    active_cap = int(getattr(settings, "ACTIVE_JOB_CAP", 20))
    active_count = BatchJob.objects.filter(
        status__in=[BatchJobStatus.PENDING, BatchJobStatus.IN_PROGRESS]
    ).count()
    if active_count >= active_cap:
        return _quota_response("Active job limit exceeded.")

    return None


def reserve_job_create_quota(request: HttpRequest, row_count: int) -> JsonResponse | None:
    from apps.batches.models import BatchQuotaEvent

    principal = _principal(request)
    ip_address = _client_ip(request)
    with transaction.atomic():
        _acquire_quota_advisory_locks(
            BatchQuotaEvent.KIND_JOB_CREATE,
            principal,
            ip_address,
        )
        quota_error = validate_job_create_controls(request, row_count)
        if quota_error is not None:
            return quota_error
        BatchQuotaEvent.objects.create(
            kind=BatchQuotaEvent.KIND_JOB_CREATE,
            principal=principal,
            ip_address=ip_address,
            row_count=row_count,
        )
    return None


def create_job_under_quota(
    request: HttpRequest,
    rows: list[dict[str, object]],
    *,
    remote_enabled: bool,
) -> tuple[BatchJob | None, str, JsonResponse | None]:
    """Create a job while holding quota locks through the active-job insert.

    Production routes must NOT call this function when async ingest is enabled.
    It is retained for service-level tests and diagnostics only; set
    ``INGEST_DEFER_HEAVY_PROCESSING = False`` in any test that needs the
    synchronous pipeline.
    """
    if getattr(settings, "INGEST_DEFER_HEAVY_PROCESSING", True):
        raise RuntimeError(
            "create_job_under_quota is disabled when async ingest is enabled"
        )

    from apps.batches import services
    from apps.batches.models import BatchQuotaEvent

    principal = _principal(request)
    ip_address = _client_ip(request)
    with transaction.atomic():
        _acquire_quota_advisory_locks(
            BatchQuotaEvent.KIND_JOB_CREATE,
            principal,
            ip_address,
        )
        quota_error = validate_job_create_controls(request, len(rows))
        if quota_error is not None:
            return None, "", quota_error

        job, plaintext_secret = services.create_job_records(
            rows,
            remote_enabled=remote_enabled,
            remote_principal=principal,
        )
        BatchQuotaEvent.objects.create(
            kind=BatchQuotaEvent.KIND_JOB_CREATE,
            principal=principal,
            ip_address=ip_address,
            row_count=len(rows),
        )

    services.process_job_items(job)
    return job, plaintext_secret, None


def stage_job_under_quota(
    request: HttpRequest,
    row_iter: object,
    *,
    remote_enabled: bool,
) -> tuple[BatchJob | None, str, JsonResponse | None]:
    """Stage a job WITHOUT running heavy processing (deferred ingest path, F-11).

    Streams the parser ``row_iter`` straight into BatchItems via
    ``create_job_records_streaming`` — the whole row list is never materialised —
    leaving the job in the claimable PARSING state for the durable
    ``process_jobs`` worker. The HTTP request returns immediately.

    Quota controls that need a row count up front (max-rows, daily cap) are
    enforced against the actual streamed count: per-job ``MAX_ROWS_PER_JOB`` is a
    hard mid-stream cap (rolls back + 413); the daily cap is re-checked after the
    real count is known and the job is rolled back if it would be exceeded.
    """
    from apps.batches import services
    from apps.batches.models import BatchQuotaEvent
    from apps.batches.services import RowCapExceededError

    principal = _principal(request)
    ip_address = _client_ip(request)
    max_rows = int(getattr(settings, "MAX_ROWS_PER_JOB", 10_000))
    daily_cap = int(getattr(settings, "JOB_CREATE_DAILY_ROW_CAP", 100_000))
    day_ago = timezone.now() - timedelta(days=1)

    try:
        with transaction.atomic():
            _acquire_quota_advisory_locks(
                BatchQuotaEvent.KIND_JOB_CREATE,
                principal,
                ip_address,
            )
            # Non-row-count controls (disk, rate, active cap) up front; the
            # row-count-dependent caps are enforced after streaming.
            quota_error = validate_job_create_controls(request, 0)
            if quota_error is not None:
                return None, "", quota_error

            job, plaintext_secret, row_count = services.create_job_records_streaming(
                row_iter,  # type: ignore[arg-type]
                remote_enabled=remote_enabled,
                remote_principal=principal,
                max_rows=max_rows,
            )

            used_rows = (
                BatchQuotaEvent.objects.filter(
                    kind__in=[
                        BatchQuotaEvent.KIND_JOB_CREATE,
                        BatchQuotaEvent.KIND_JOB_ROWS,
                    ],
                    created_at__gte=day_ago,
                    principal=principal,
                ).aggregate(total=Sum("row_count"))["total"]
                or 0
            )
            if used_rows + row_count > daily_cap:
                transaction.set_rollback(True)
                return None, "", _quota_response("Daily row quota exceeded.")

            BatchQuotaEvent.objects.create(
                kind=BatchQuotaEvent.KIND_JOB_CREATE,
                principal=principal,
                ip_address=ip_address,
                row_count=row_count,
            )
    except RowCapExceededError as exc:
        return None, "", _quota_response(
            f"Job has more than {exc.max_rows} rows; the maximum is {exc.max_rows}.",
            status=413,
        )

    # NB: no process_job_items — the durable worker drains the PARSING job.
    return job, plaintext_secret, None


def validate_job_create_controls_no_rowcount(
    request: HttpRequest,
) -> JsonResponse | None:
    """Row-count-INDEPENDENT job-create controls (disk, rate, active-job cap).

    Used by the spooled-upload path (H-04): when parsing is deferred to the
    ingest worker the row count is not known in-request, so the row-count caps
    (``MAX_ROWS_PER_JOB`` hard cap, ``JOB_CREATE_DAILY_ROW_CAP`` daily cap) are
    enforced later by :func:`apps.batches.services.stage_spooled_upload`. This
    function still enforces every control that does NOT need a row count, so a
    flood of zero-row uploads (rate, active-job count, disk pressure) is still
    rejected up front.
    """
    return validate_job_create_controls(request, 0)


def spool_upload_under_quota(
    request: HttpRequest,
    upload: object,
    *,
    remote_enabled: bool,
    is_text: bool = False,
) -> tuple[BatchJob | None, str, JsonResponse | None]:
    """Atomically spool an upload and create a BatchJob in UPLOADING (H-04).

    The HTTP request does NO parsing/RDKit/BatchItem creation: it enforces the
    row-count-independent quota controls (under the same advisory locks the
    synchronous path uses), records a quota event, atomically spools the raw
    bytes to a durable artifact, and creates the job in the claimable
    ``UPLOADING`` state. The durable ``process_jobs`` worker later parses the
    artifact (and enforces the row-count caps) via ``stage_spooled_upload``.

    Returns ``(job, plaintext_secret, error_response)`` mirroring
    ``stage_job_under_quota``.
    """
    import secrets

    from django.contrib.auth.hashers import make_password

    from apps.batches.ingest import (
        UploadTooLarge,
        spool_text_input,
        spool_upload_file,
        unlink_upload_artifact,
    )
    from apps.batches.models import BatchJob, BatchQuotaEvent
    from apps.core.enums import BatchProcessingState
    from apps.remote.budget import normalize_principal

    principal = _principal(request)
    stored_principal = normalize_principal(principal)
    ip_address = _client_ip(request)

    with transaction.atomic():
        _acquire_quota_advisory_locks(
            BatchQuotaEvent.KIND_JOB_CREATE,
            principal,
            ip_address,
        )
        quota_error = validate_job_create_controls_no_rowcount(request)
        if quota_error is not None:
            return None, "", quota_error

        max_spool_bytes, retained_quota_error = _retained_upload_allowance(
            stored_principal
        )

        try:
            if is_text:
                spooled = spool_text_input(  # type: ignore[arg-type]
                    upload,
                    max_bytes=max_spool_bytes,
                )
            else:
                spooled = spool_upload_file(upload, max_bytes=max_spool_bytes)
        except UploadTooLarge as exc:
            transaction.set_rollback(True)
            if retained_quota_error is not None:
                return None, "", retained_quota_error
            return None, "", _quota_response(str(exc), status=413)

        # The artifact is now on disk OUTSIDE the DB transaction. If ANY later
        # step fails the atomic block rolls the row back but the spooled file
        # would remain — compensate by unlinking it so no orphan survives (R-07).
        try:
            plaintext_secret = secrets.token_urlsafe(32)
            job = BatchJob.objects.create(
                secret_hash=make_password(plaintext_secret),
                remote_enabled=remote_enabled,
                remote_principal=stored_principal,
                processing_state=BatchProcessingState.UPLOADING,
                upload_artifact_path=spooled.relative_path,
                upload_original_name=spooled.original_name,
                upload_content_type=spooled.content_type,
                upload_size_bytes=spooled.size_bytes,
                upload_sha256=spooled.sha256,
            )
            # The quota event is recorded with a zero row count up front; the
            # worker re-records the real row count when it stages the artifact.
            BatchQuotaEvent.objects.create(
                kind=BatchQuotaEvent.KIND_JOB_CREATE,
                principal=principal,
                ip_address=ip_address,
                row_count=0,
            )
        except Exception:
            unlink_upload_artifact(spooled.relative_path)
            raise

    return job, plaintext_secret, None


def record_job_create_event(request: HttpRequest, row_count: int) -> None:
    from apps.batches.models import BatchQuotaEvent

    BatchQuotaEvent.objects.create(
        kind=BatchQuotaEvent.KIND_JOB_CREATE,
        principal=_principal(request),
        ip_address=_client_ip(request),
        row_count=row_count,
    )


def validate_export_create_controls(request: HttpRequest) -> JsonResponse | None:
    from apps.batches.models import BatchQuotaEvent

    limit = int(getattr(settings, "EXPORT_CREATE_RATE_LIMIT_PER_MINUTE", 10))
    now = timezone.now()
    minute_ago = now - timedelta(minutes=1)
    principal = _principal(request)
    ip_address = _client_ip(request)
    recent_by_principal = BatchQuotaEvent.objects.filter(
        kind=BatchQuotaEvent.KIND_EXPORT_CREATE,
        principal=principal,
        created_at__gte=minute_ago,
    ).count()
    recent_by_ip = BatchQuotaEvent.objects.filter(
        kind=BatchQuotaEvent.KIND_EXPORT_CREATE,
        ip_address=ip_address,
        created_at__gte=minute_ago,
    ).count()
    if limit >= 0 and (recent_by_principal >= limit or recent_by_ip >= limit):
        return _quota_response("Export rate limit exceeded.")
    return None


def reserve_export_create_quota(request: HttpRequest) -> JsonResponse | None:
    from apps.batches.models import BatchQuotaEvent

    principal = _principal(request)
    ip_address = _client_ip(request)
    with transaction.atomic():
        _acquire_quota_advisory_locks(
            BatchQuotaEvent.KIND_EXPORT_CREATE,
            principal,
            ip_address,
        )
        quota_error = validate_export_create_controls(request)
        if quota_error is not None:
            return quota_error
        BatchQuotaEvent.objects.create(
            kind=BatchQuotaEvent.KIND_EXPORT_CREATE,
            principal=principal,
            ip_address=ip_address,
        )
    return None


def record_export_create_event(request: HttpRequest) -> None:
    from apps.batches.models import BatchQuotaEvent

    BatchQuotaEvent.objects.create(
        kind=BatchQuotaEvent.KIND_EXPORT_CREATE,
        principal=_principal(request),
        ip_address=_client_ip(request),
    )


# ---------------------------------------------------------------------------
# Upload safety guards
# ---------------------------------------------------------------------------


class UploadTooLargeError(ValueError):
    """File or payload exceeds MAX_UPLOAD_MB."""


class RowTooLongError(ValueError):
    """A single row/structure exceeds the maximum allowed length."""


class SdfPropertyTooLargeError(ValueError):
    """An SDF property value exceeds the maximum allowed size."""


class DecompressionBombError(ValueError):
    """Decompressed size exceeds the allowed ratio vs compressed size."""


def check_upload_size(content_length: int, max_mb: int | None = None) -> None:
    """Raise UploadTooLargeError if content_length exceeds MAX_UPLOAD_MB.

    Does NOT log the content; only the size is mentioned in the error.
    """
    limit_mb = max_mb if max_mb is not None else getattr(settings, "MAX_UPLOAD_MB", 100)
    limit_bytes = limit_mb * 1024 * 1024
    if content_length > limit_bytes:
        raise UploadTooLargeError(
            f"Upload size {content_length} bytes exceeds limit of {limit_mb} MB."
        )


def check_structure_length(value: str, field_name: str = "structure") -> None:
    """Raise RowTooLongError if a structure string is too long."""
    if len(value) > MAX_STRUCTURE_LEN:
        raise RowTooLongError(
            f"Field '{field_name}' exceeds maximum length of {MAX_STRUCTURE_LEN} characters."
        )


def check_sdf_property_length(value: str, prop_name: str = "property") -> None:
    """Raise SdfPropertyTooLargeError if an SDF property value is too large."""
    if len(value) > MAX_SDF_PROPERTY_LEN:
        raise SdfPropertyTooLargeError(
            f"SDF property '{prop_name}' exceeds maximum length of {MAX_SDF_PROPERTY_LEN} characters."
        )


def check_decompression_ratio(
    compressed_data: bytes,
    max_ratio: float | None = None,
) -> bytes:
    """Decompress zip data and reject if expansion ratio is too high (zip-bomb guard).

    Counts REAL decompressed bytes by streaming each member in chunks — never
    trusts attacker-controlled central-directory metadata (info.file_size).

    Returns the decompressed bytes of the first member on success.
    Raises DecompressionBombError if the running total of real decompressed
    bytes exceeds compressed_size * ratio_limit before reading is complete.
    Raises ValueError for non-zip data or corrupt archives.
    """
    ratio_limit = max_ratio if max_ratio is not None else MAX_DECOMPRESSION_RATIO
    compressed_size = len(compressed_data)

    if compressed_size == 0:
        return b""

    limit = compressed_size * ratio_limit
    total_real: int = 0
    first_member_chunks: list[bytes] = []
    first_name: str | None = None

    try:
        buf = io.BytesIO(compressed_data)
        with zipfile.ZipFile(buf, "r") as zf:
            for info in zf.infolist():
                is_first = first_name is None
                if is_first:
                    first_name = info.filename
                with zf.open(info) as member:
                    while True:
                        chunk = member.read(65536)
                        if not chunk:
                            break
                        total_real += len(chunk)
                        if total_real > limit:
                            raise DecompressionBombError(
                                f"Decompression ratio exceeds {ratio_limit}x limit "
                                f"(real bytes > {int(limit)})."
                            )
                        if is_first:
                            first_member_chunks.append(chunk)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Invalid zip archive: {exc}") from exc

    if first_name is None:
        return b""
    return b"".join(first_member_chunks)


# ---------------------------------------------------------------------------
# Log redaction
# ---------------------------------------------------------------------------

_REDACTED = "[REDACTED]"


def redact_for_log(value: str, *, max_chars: int = 16) -> str:
    """Return a safe string for logging: truncate long values, redact secrets.

    Any value over max_chars characters is considered a potential structure or
    secret and is replaced with [REDACTED:<length>].  Short values (e.g. error
    codes, status strings) are passed through.
    """
    if len(value) > max_chars:
        return f"{_REDACTED}:{len(value)}chars"
    return value


class SecretRedactionFilter(logging.Filter):
    """Logging filter that redacts job secrets and export tokens from log lines.

    Replaces:
    - ``?secret=<value>`` / ``&secret=<value>`` query parameters with
      ``?secret=[REDACTED]`` / ``&secret=[REDACTED]``.
    - ``/api/exports/<token>`` path segments with ``/api/exports/[REDACTED]``.
    - ``/jobs/<job_id>/claim/<secret>`` capability secrets carried IN the PATH
      (Django's "Forbidden: <path>" request-log lines expose these on a 403).
    - legacy ``/jobs/<job_id>/<secret>[/items|/progress]`` path secrets.
    - legacy ``/items/<item_id>/<secret>/structure`` path secrets.
    - ``/api/jobs/<uuid>/...?secret=<value>`` (covered by the query rule too, but
      matched explicitly for defence in depth).

    Attach to any handler that writes Django request/access log lines to
    prevent plaintext secrets reaching log storage.
    """

    QUERY_SECRET_RE = re.compile(r"([?&]secret=)[^&\s]+")
    EXPORT_PATH_RE = re.compile(r"(/api/exports/)[^/?\s]+")
    CLAIM_PATH_RE = re.compile(r"(/jobs/[^/?\s]+/claim/)[^/?\s]+")
    # A path ends at a query/fragment, whitespace, end of string, or the
    # punctuation that typically wraps a path inside a logged message
    # (quotes, commas, brackets), so repr'd/quoted paths are redacted too.
    _PATH_END = r"(?:[?#\s'\",)>\]]|$)"
    # (?<!/api) keeps the JSON API routes (/api/jobs/<id>/exports, ...), which
    # carry no path secret, out of the legacy capability rules.
    LEGACY_JOB_PATH_RE = re.compile(
        r"(?<!/api)(/jobs/[^/?#\s]+/)"
        r"(?!(?:items|progress)/?" + _PATH_END + r")"
        r"[^/?#\s'\",)>\]]+"
        r"(?=(?:/(?:items|progress))?/?" + _PATH_END + r")"
    )
    LEGACY_ITEM_PATH_RE = re.compile(
        r"(?<!/api)(/items/[0-9]+/)[^/?#\s'\",)>\]]+(?=/structure/?" + _PATH_END + r")"
    )
    API_JOB_SECRET_RE = re.compile(
        r"(/api/jobs/[0-9a-fA-F-]{36}/[^?\s]*[?&]secret=)[^&\s]+"
    )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        message = self.QUERY_SECRET_RE.sub(r"\1[REDACTED]", message)
        message = self.EXPORT_PATH_RE.sub(r"\1[REDACTED]", message)
        message = self.CLAIM_PATH_RE.sub(r"\1[REDACTED]", message)
        message = self.LEGACY_JOB_PATH_RE.sub(r"\1[REDACTED]", message)
        message = self.LEGACY_ITEM_PATH_RE.sub(r"\1[REDACTED]", message)
        message = self.API_JOB_SECRET_RE.sub(r"\1[REDACTED]", message)
        record.msg = message
        record.args = ()
        return True
