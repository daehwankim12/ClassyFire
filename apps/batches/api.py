"""Batch jobs REST API.

Endpoints
---------
POST /api/jobs
    Create a batch job from direct text input and/or an uploaded file.
    Returns: {"job_id": "<uuid>", "result_secret": "<token>"}

    Request body (JSON):
        text_input    string | null   — newline-separated rows
        remote_enabled  bool | null   — default: settings.REMOTE_ENABLED

    Status codes:
        201 Created    — job accepted and local processing complete
        400 Bad Request — validation error

GET /api/jobs/{id}?secret=<plaintext>
    Return job status JSON.

GET /api/jobs/{id}/items?secret=<plaintext>&page=1&page_size=100&status_filter=&search=
    Return paginated + filtered items JSON.

POST /api/items/{id}/structure
    Body: {"structure": "...", "secret": "..."}
    Submit a structure for a NEED_STRUCTURE item.

POST /api/jobs/{id}/exports
    Body: {"format": "csv"|"tsv"|"json"|"csv.gz"|"tsv.gz", "secret": "..."}
    Create an export and return {"token": "...", "expires_at": "..."}.

GET /api/exports/{token}
    Download an export by token.
"""
from __future__ import annotations

import json

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import (
    FileResponse,
    HttpRequest,
    HttpResponseNotFound,
    JsonResponse,
)
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.batches.exports import (
    create_export_for_authorized_job,
    get_export_path,
)
from apps.batches.models import BatchItem, BatchJob
from apps.batches.remote_delay import (
    build_remote_delayed_breakdown,
    next_action_for_remote_delayed,
)
from apps.batches.services import (
    StructureKeyMismatchError,
    StructureSubmissionConflictError,
    StructureSubmissionError,
    submit_structure_for_item,
)
from apps.batches.storage import get_artifact_storage
from apps.core.enums import (
    CLAIMABLE_PROCESSING_STATES,
    BatchItemStatus,
    BatchProcessingState,
)
from apps.core.security import (
    UploadTooLargeError,
    check_upload_size,
    request_has_job_access_cookie,
    request_has_shared_access,
    require_csrf_for_cookie_auth,
    require_shared_access,
    reserve_export_create_quota,
    spool_upload_under_quota,
)


def _job_secret(request: HttpRequest, body: dict[str, object] | None = None) -> str:
    """Return the job secret from the request, checking in this priority order:

    1. ``X-Job-Secret`` request header — keeps the secret out of URLs/query
       strings and therefore out of access logs (preferred for API clients).
    2. ``body["secret"]`` — accepted for POST/JSON callers.
    3. ``?secret=`` query parameter — REJECTED; raises ValueError so the caller
       can return HTTP 400 directing the client to use the header form.
    """
    header_secret = request.headers.get("X-Job-Secret", "")
    if header_secret:
        return header_secret
    if body is not None:
        return str(body.get("secret") or "")
    if request.GET.get("secret"):
        raise ValueError(
            "Use X-Job-Secret header or JSON body secret; query secrets are not accepted."
        )
    return ""


# Status groups for items filter
FOUND_STATUSES = [
    BatchItemStatus.FOUND_LIVE_CACHE,
    BatchItemStatus.FOUND_WISHART_ENTITY,
    BatchItemStatus.COMPLETE_AFTER_WISHART,
]
INVALID_STATUSES = [
    BatchItemStatus.INVALID_INPUT,
    BatchItemStatus.RESULT_KEY_MISMATCH,
    BatchItemStatus.REMOTE_TIMED_OUT,
]
STATUS_FILTER_MAP: dict[str, list[BatchItemStatus]] = {
    "FOUND": FOUND_STATUSES,
    "PENDING": [BatchItemStatus.PENDING_CLASSIFICATION],
    "NEED_STRUCTURE": [BatchItemStatus.NEED_STRUCTURE],
    "INVALID": INVALID_STATUSES,
    "DELAYED": [BatchItemStatus.REMOTE_DELAYED],
}


@csrf_exempt
@require_shared_access
@require_csrf_for_cookie_auth
@require_http_methods(["POST"])
def create_job_view(request: HttpRequest) -> JsonResponse:
    """POST /api/jobs — create a batch job and run local processing.

    Accepts EITHER a JSON body (``text_input``) OR a multipart upload with an
    ``input_file`` field. A multipart file is parsed as a BOUNDED STREAM, format
    dispatched on suffix+content-type (CSV/TSV/SDF/text) exactly like the web
    submit route (F-01).
    """
    content_type = request.content_type or ""
    is_multipart = content_type.startswith("multipart/form-data")

    if is_multipart:
        raw_remote = request.POST.get("remote_enabled")
        text_input = request.POST.get("text_input") or ""
    else:
        try:
            body = json.loads(request.body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return JsonResponse({"error": f"Invalid JSON: {exc}"}, status=400)
        raw_remote = body.get("remote_enabled")
        text_input = body.get("text_input") or ""

    # --- remote_enabled ---
    # In a multipart form an absent checkbox arrives as "" (or is missing
    # entirely), so for that branch ""→False matches HTML-form semantics. For a
    # JSON client we preserve the original behavior: only the explicit
    # false/0/no literals disable remote (an empty string stays truthy).
    if raw_remote is None:
        remote_enabled: bool = getattr(settings, "REMOTE_ENABLED", True)
    elif isinstance(raw_remote, bool):
        remote_enabled = raw_remote
    elif isinstance(raw_remote, str):
        falsey = ("false", "0", "no", "") if is_multipart else ("false", "0", "no")
        remote_enabled = raw_remote.lower() not in falsey
    else:
        remote_enabled = bool(raw_remote)

    # Guard: reject payloads whose text_input exceeds MAX_UPLOAD_MB.
    # Check the encoded byte length so the limit is consistent with the file-
    # upload path in views.py.  Do NOT log the content — only the byte size.
    try:
        check_upload_size(len(text_input.encode("utf-8")))
    except UploadTooLargeError as exc:
        return JsonResponse({"error": str(exc)}, status=413)

    # --- Multipart file upload: reject oversize before reading content. ---
    file = None
    if is_multipart:
        file = request.FILES.get("input_file")
        # Reject an ambiguous submission that provides BOTH a file and pasted
        # text: spooling only the file would silently drop the pasted text.
        if file is not None and text_input.strip():
            return JsonResponse(
                {"error": "Provide either pasted text or a file upload, not both."},
                status=400,
            )
        if file is not None:
            try:
                check_upload_size(file.size if file.size is not None else 0)
            except UploadTooLargeError as exc:
                return JsonResponse({"error": str(exc)}, status=413)

    # H-04: BOTH a FILE upload and a JSON/text paste are atomically SPOOLED to a
    # durable artifact and the job is created in UPLOADING — NO
    # parsing/RDKit/BatchItem creation in the request (returns 202 BEFORE any
    # parsing). The durable `process_jobs` worker parses the artifact later via
    # the claim-fenced `stage_spooled_upload`, where the row-count caps (and
    # bounded scanner) are enforced.
    if file is not None:
        job, plaintext_secret, quota_error = spool_upload_under_quota(
            request,
            file,
            remote_enabled=remote_enabled,
        )
    else:
        job, plaintext_secret, quota_error = spool_upload_under_quota(
            request,
            text_input,
            remote_enabled=remote_enabled,
            is_text=True,
        )
    if quota_error is not None:
        return quota_error

    if job is None:
        return JsonResponse({"error": "Job creation failed."}, status=500)

    return JsonResponse(
        {
            "job_id": str(job.pk),
            "result_secret": plaintext_secret,
            "status": job.status,
            "processing_state": job.processing_state,
        },
        status=202,
    )


@csrf_exempt
@require_shared_access
@require_http_methods(["GET"])
def job_status_view(request: HttpRequest, job_id: str) -> JsonResponse:
    """GET /api/jobs/{id} — return job status JSON.

    Secret must be supplied via the X-Job-Secret header; query-string secrets
    are no longer accepted and return HTTP 400.
    """
    job = get_object_or_404(BatchJob, pk=job_id)
    try:
        secret = _job_secret(request)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    if secret:
        if not check_password(secret, job.secret_hash):
            return JsonResponse({"error": "Invalid secret."}, status=403)
    elif not request_has_job_access_cookie(request, job.pk):
        return JsonResponse({"error": "Invalid secret."}, status=403)

    # Operational counts — computed from live item rows so callers know exactly
    # what work is outstanding regardless of whether model counters have been
    # flushed.  A single pass with three .count() calls avoids N+1.
    pending_normalization = job.items.filter(
        status=BatchItemStatus.PENDING_NORMALIZATION
    ).count()
    remote_delayed = job.items.filter(status=BatchItemStatus.REMOTE_DELAYED).count()
    need_structure = job.items.filter(status=BatchItemStatus.NEED_STRUCTURE).count()
    remote_delayed_breakdown = (
        build_remote_delayed_breakdown(job) if remote_delayed else []
    )

    if pending_normalization:
        next_action = "wait_for_ingest_worker"
    elif remote_delayed:
        next_action = next_action_for_remote_delayed(remote_delayed_breakdown)
    elif need_structure:
        next_action = "submit_missing_structures"
    elif job.processing_state in CLAIMABLE_PROCESSING_STATES:
        # Deferred work can be claimable before any BatchItem rows exist (e.g. a
        # freshly spooled upload, or a REMOTE_PENDING job whose delayed rows were
        # already drained). Report the accurate next step instead of "none".
        # REMOTE_PENDING is waiting on remote capacity, not the ingest worker.
        if job.processing_state == BatchProcessingState.REMOTE_PENDING:
            next_action = "wait_for_remote_capacity"
        else:
            next_action = "wait_for_ingest_worker"
    else:
        next_action = "none"

    return JsonResponse(
        {
            "id": str(job.pk),
            "status": job.status,
            "processing_state": job.processing_state,
            "total_count": job.total_count,
            "completed_count": job.completed_count,
            "local_hit_count": job.local_hit_count,
            "remote_pending_count": job.remote_pending_count,
            "need_structure_count": need_structure,
            "normalization_pending_count": pending_normalization,
            "remote_delayed_count": remote_delayed,
            "remote_delayed_breakdown": remote_delayed_breakdown,
            "invalid_count": job.invalid_count,
            "remote_enabled": job.remote_enabled,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "next_action": next_action,
        }
    )


@csrf_exempt
@require_shared_access
@require_http_methods(["GET"])
def job_items_view(request: HttpRequest, job_id: str) -> JsonResponse:
    """GET /api/jobs/{id}/items — paginated + filtered items JSON.

    Secret must be supplied via the X-Job-Secret header; query-string secrets
    are no longer accepted and return HTTP 400.
    """
    job = get_object_or_404(BatchJob, pk=job_id)
    try:
        secret = _job_secret(request)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    if secret:
        if not check_password(secret, job.secret_hash):
            return JsonResponse({"error": "Invalid secret."}, status=403)
    elif not request_has_job_access_cookie(request, job.pk):
        return JsonResponse({"error": "Invalid secret."}, status=403)

    status_filter = request.GET.get("status_filter", "")
    search = request.GET.get("search", "")
    try:
        page_number = max(1, int(request.GET.get("page", 1)))
        page_size = min(max(1, int(request.GET.get("page_size", 100))), 500)
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid page or page_size."}, status=400)

    qs = BatchItem.objects.filter(job=job).order_by("row_order")
    if status_filter and status_filter in STATUS_FILTER_MAP:
        qs = qs.filter(status__in=STATUS_FILTER_MAP[status_filter])
    if search:
        qs = qs.filter(
            Q(original_input__icontains=search) | Q(external_id__icontains=search)
        )

    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(page_number)

    items_data = [
        {
            "id": item.pk,
            "row_order": item.row_order,
            "external_id": item.external_id,
            "original_input": item.original_input,
            "detected_type": item.detected_type,
            "inchikey": item.inchikey,
            "status": item.status,
            "message": item.message,
        }
        for item in page_obj
    ]

    return JsonResponse(
        {
            "count": paginator.count,
            "num_pages": paginator.num_pages,
            "page": page_obj.number,
            "results": items_data,
        }
    )


@csrf_exempt
@require_csrf_for_cookie_auth
@require_http_methods(["POST"])
def item_structure_view(request: HttpRequest, item_id: int) -> JsonResponse:
    """POST /api/items/{id}/structure — submit a structure for a NEED_STRUCTURE item."""
    item = get_object_or_404(BatchItem, pk=item_id)
    job = item.job

    if not request_has_shared_access(request, job.pk):
        return JsonResponse(
            {"error": "Authentication required."},
            status=401,
            headers={"WWW-Authenticate": 'Bearer realm="ClassyFire"'},
        )

    try:
        body = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return JsonResponse({"error": f"Invalid JSON: {exc}"}, status=400)

    secret = _job_secret(request, body)
    if secret:
        if not check_password(secret, job.secret_hash):
            return JsonResponse({"error": "Invalid secret."}, status=403)
    elif not request_has_job_access_cookie(request, job.pk):
        return JsonResponse({"error": "Invalid secret."}, status=403)

    structure = body.get("structure", "").strip()
    if not structure:
        return JsonResponse({"error": "structure is required"}, status=400)

    try:
        item = submit_structure_for_item(item.pk, structure)
    except StructureKeyMismatchError as exc:
        # FR-015: user submitted a structure whose InChIKey doesn't match the
        # expected InChIKey.  Leave the item as NEED_STRUCTURE (retryable) and
        # return a 422 error — do NOT permanently strand it as RESULT_KEY_MISMATCH
        # (that status is reserved for remote-response mismatches, FR-018).
        return JsonResponse(
            {
                "error": str(exc),
                "submitted_inchikey": exc.submitted_inchikey,
                "expected_inchikey": exc.expected_inchikey,
            },
            status=422,
        )
    except StructureSubmissionConflictError as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    except StructureSubmissionError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    return JsonResponse({"status": item.status})


@csrf_exempt
@require_shared_access
@require_csrf_for_cookie_auth
@require_http_methods(["POST"])
def create_export_view(request: HttpRequest, job_id: str) -> JsonResponse:
    """POST /api/jobs/{id}/exports — create or reuse an export."""
    job = get_object_or_404(BatchJob, pk=job_id)

    try:
        body = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return JsonResponse({"error": f"Invalid JSON: {exc}"}, status=400)

    secret = _job_secret(request, body)
    fmt = body.get("format", "")

    if not fmt:
        return JsonResponse({"error": "format is required"}, status=400)

    try:
        if secret:
            if not check_password(secret, job.secret_hash):
                return JsonResponse({"error": "Invalid secret."}, status=403)
            quota_error = reserve_export_create_quota(request)
            if quota_error is not None:
                return quota_error
            token, expires_at = create_export_for_authorized_job(job, str(fmt))
        elif request_has_job_access_cookie(request, job.pk):
            quota_error = reserve_export_create_quota(request)
            if quota_error is not None:
                return quota_error
            token, expires_at = create_export_for_authorized_job(job, str(fmt))
        else:
            return JsonResponse({"error": "Invalid secret."}, status=403)
    except PermissionError:
        return JsonResponse({"error": "Invalid secret."}, status=403)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    return JsonResponse({"token": token, "expires_at": expires_at}, status=201)


@require_http_methods(["GET"])
def download_export_view(
    request: HttpRequest,
    token: str,
) -> FileResponse | HttpResponseNotFound:
    """GET /api/exports/{token} — download an export by capability token.

    The token is a 256-bit ``secrets.token_urlsafe(32)`` value, issued only to an
    authenticated ``create_export`` call, job-scoped and expiring. A valid,
    non-expired token IS the authorization: the file is served on the token
    alone — no ``?secret=``, header, cookie, or session is required. This lets a
    browser download via a plain link with no secret in the URL.

    Unknown or expired tokens return 404 (not 401/403) so the response never
    reveals the auth model or whether a token exists.
    """
    path = get_export_path(token)
    if path is None:
        return HttpResponseNotFound("Export not found or expired.")

    # Determine content type from extension
    name = path.name
    if name.endswith((".csv.gz", ".tsv.gz")):
        content_type = "application/gzip"
    elif name.endswith(".csv"):
        content_type = "text/csv"
    elif name.endswith(".tsv"):
        content_type = "text/tab-separated-values"
    elif name.endswith(".json"):
        content_type = "application/json"
    else:
        content_type = "application/octet-stream"

    response = FileResponse(get_artifact_storage().open(path, "rb"), content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{path.name}"'
    return response
