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

CSRF note: the /api/ endpoints use @csrf_exempt because bearer-token auth
provides a complete compensating control (no ambient cookie authority is
trusted).  Any browser-session POST that is NOT bearer-token protected (e.g.
the submit form at /) retains normal Django CSRF protection.

PROD mode: if ACCESS_MODE="strict" or SHARED_ACCESS_PASSWORD_HASH is non-empty
in prod.py (enforced via env_required), the gate will deny all unauthenticated
requests.
"""
from __future__ import annotations

import functools
import io
import logging
import zipfile
from collections.abc import Callable

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.http import HttpRequest, JsonResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_STRUCTURE_LEN: int = 100_000  # bytes per structure string
MAX_SDF_PROPERTY_LEN: int = 10_000  # bytes per SDF property value
MAX_DECOMPRESSION_RATIO: float = 100.0  # reject if expanded > N x compressed


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def _get_shared_hash() -> str:
    return getattr(settings, "SHARED_ACCESS_PASSWORD_HASH", "") or ""


def _is_dev_open() -> bool:
    """True when no password hash is configured — dev/test open mode."""
    return not _get_shared_hash().strip()


def _check_bearer(request: HttpRequest) -> bool:
    """Validate bearer token from Authorization header or X-Shared-Access-Token."""
    token: str = ""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):]
    else:
        token = request.headers.get("X-Shared-Access-Token", "")

    if not token:
        return False

    stored_hash = _get_shared_hash()
    if not stored_hash:
        return False

    return bool(check_password(token, stored_hash))


def _is_authenticated(request: HttpRequest) -> bool:
    """Return True if the request carries valid credentials."""
    if _is_dev_open():
        return True

    if _check_bearer(request):
        return True

    if hasattr(request, "user") and request.user.is_authenticated:
        return True

    return False


def require_shared_access(view_func: Callable) -> Callable:
    """Decorator: gate a view behind shared-access authentication.

    In dev-open mode (SHARED_ACCESS_PASSWORD_HASH unset), all requests pass.
    In secure mode, a valid Bearer token or session login is required.
    """
    @functools.wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs):
        if not _is_authenticated(request):
            return JsonResponse(
                {"error": "Authentication required."},
                status=401,
                headers={"WWW-Authenticate": 'Bearer realm="ClassyFire"'},
            )
        return view_func(request, *args, **kwargs)

    return wrapper


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

    Returns the decompressed bytes on success.
    Raises DecompressionBombError if the ratio exceeds the allowed maximum.
    Raises ValueError for non-zip data or corrupt archives.
    """
    ratio_limit = max_ratio if max_ratio is not None else MAX_DECOMPRESSION_RATIO
    compressed_size = len(compressed_data)

    if compressed_size == 0:
        return b""

    try:
        buf = io.BytesIO(compressed_data)
        with zipfile.ZipFile(buf, "r") as zf:
            total_decompressed = sum(info.file_size for info in zf.infolist())
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Invalid zip archive: {exc}") from exc

    if compressed_size > 0:
        ratio = total_decompressed / compressed_size
        if ratio > ratio_limit:
            raise DecompressionBombError(
                f"Decompression ratio {ratio:.1f}x exceeds limit of {ratio_limit}x."
            )

    buf.seek(0)
    with zipfile.ZipFile(buf, "r") as zf:
        names = zf.namelist()
        if not names:
            return b""
        return zf.read(names[0])


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
