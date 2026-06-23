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

from apps.batches.exports import create_export, get_export_path
from apps.batches.ingest import parse_text_input
from apps.batches.models import BatchItem, BatchJob
from apps.batches.services import create_job, recompute_job_counters
from apps.core.enums import BatchItemStatus
from apps.core.security import UploadTooLargeError, check_upload_size, require_shared_access


def _job_secret(request: HttpRequest, body: dict[str, object] | None = None) -> str:
    """Return the job secret from the request, checking in this priority order:

    1. ``X-Job-Secret`` request header — keeps the secret out of URLs/query
       strings and therefore out of access logs (preferred for API clients).
    2. ``body["secret"]`` — accepted for backwards-compat POST/JSON callers.
    3. ``?secret=`` query parameter — preserved for the shareable-link use case
       (§9.2: result URLs embed the secret so recipients can open them directly).
    """
    header_secret = request.headers.get("X-Job-Secret", "")
    if header_secret:
        return header_secret
    if body is not None:
        body_secret = str(body.get("secret") or "")
        if body_secret:
            return body_secret
    return request.GET.get("secret", "")


# Status groups for items filter
FOUND_STATUSES = [
    BatchItemStatus.FOUND_LIVE_CACHE,
    BatchItemStatus.FOUND_SEED_CACHE,
    BatchItemStatus.FOUND_WISHART_ENTITY,
    BatchItemStatus.COMPLETE_AFTER_WISHART,
]
STATUS_FILTER_MAP: dict[str, list[BatchItemStatus]] = {
    "FOUND": FOUND_STATUSES,
    "PENDING": [BatchItemStatus.PENDING_CLASSIFICATION],
    "NEED_STRUCTURE": [BatchItemStatus.NEED_STRUCTURE],
    "INVALID": [BatchItemStatus.INVALID_INPUT],
    "DELAYED": [BatchItemStatus.REMOTE_DELAYED],
}


@csrf_exempt  # CSRF N/A: token-only; require_shared_access provides compensating control
@require_shared_access
@require_http_methods(["POST"])
def create_job_view(request: HttpRequest) -> JsonResponse:
    """POST /api/jobs — create a batch job and run local processing."""
    try:
        body = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return JsonResponse({"error": f"Invalid JSON: {exc}"}, status=400)

    # --- remote_enabled ---
    raw_remote = body.get("remote_enabled")
    if raw_remote is None:
        remote_enabled: bool = getattr(settings, "REMOTE_ENABLED", True)
    elif isinstance(raw_remote, bool):
        remote_enabled = raw_remote
    elif isinstance(raw_remote, str):
        remote_enabled = raw_remote.lower() not in ("false", "0", "no")
    else:
        remote_enabled = bool(raw_remote)

    # --- Collect rows ---
    text_input: str = body.get("text_input") or ""

    # Guard: reject payloads whose text_input exceeds MAX_UPLOAD_MB.
    # Check the encoded byte length so the limit is consistent with the file-
    # upload path in views.py.  Do NOT log the content — only the byte size.
    try:
        check_upload_size(len(text_input.encode("utf-8")))
    except UploadTooLargeError as exc:
        return JsonResponse({"error": str(exc)}, status=413)

    rows = list(parse_text_input(text_input))

    # Create the job and run local processing
    job, plaintext_secret = create_job(rows, remote_enabled=remote_enabled)

    return JsonResponse(
        {"job_id": str(job.pk), "result_secret": plaintext_secret},
        status=201,
    )


@csrf_exempt  # CSRF N/A: token-only; require_shared_access provides compensating control
@require_shared_access
@require_http_methods(["GET"])
def job_status_view(request: HttpRequest, job_id: str) -> JsonResponse:
    """GET /api/jobs/{id}?secret=<plaintext> — return job status JSON."""
    job = get_object_or_404(BatchJob, pk=job_id)
    secret = _job_secret(request)
    if not check_password(secret, job.secret_hash):
        return JsonResponse({"error": "Invalid secret."}, status=403)

    return JsonResponse(
        {
            "id": str(job.pk),
            "status": job.status,
            "total_count": job.total_count,
            "completed_count": job.completed_count,
            "local_hit_count": job.local_hit_count,
            "seed_hit_count": job.seed_hit_count,
            "remote_pending_count": job.remote_pending_count,
            "need_structure_count": job.need_structure_count,
            "invalid_count": job.invalid_count,
            "remote_enabled": job.remote_enabled,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        }
    )


@csrf_exempt  # CSRF N/A: token-only; require_shared_access provides compensating control
@require_shared_access
@require_http_methods(["GET"])
def job_items_view(request: HttpRequest, job_id: str) -> JsonResponse:
    """GET /api/jobs/{id}/items — paginated + filtered items JSON."""
    job = get_object_or_404(BatchJob, pk=job_id)
    secret = _job_secret(request)
    if not check_password(secret, job.secret_hash):
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


@csrf_exempt  # CSRF N/A: token-only; require_shared_access provides compensating control
@require_shared_access
@require_http_methods(["POST"])
def item_structure_view(request: HttpRequest, item_id: int) -> JsonResponse:
    """POST /api/items/{id}/structure — submit a structure for a NEED_STRUCTURE item."""
    item = get_object_or_404(BatchItem, pk=item_id)
    job = item.job

    try:
        body = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return JsonResponse({"error": f"Invalid JSON: {exc}"}, status=400)

    secret = _job_secret(request, body)
    if not check_password(secret, job.secret_hash):
        return JsonResponse({"error": "Invalid secret."}, status=403)

    structure = body.get("structure", "").strip()
    if not structure:
        return JsonResponse({"error": "structure is required"}, status=400)

    from apps.classifications.services import LookupTier, lookup_current_classification
    from apps.compounds.validators import normalize_row

    norm = normalize_row(structure)
    if norm.error or norm.inchikey is None:
        return JsonResponse(
            {"error": f"Could not parse structure: {norm.error or 'unknown'}"},
            status=400,
        )

    if item.inchikey and norm.inchikey != item.inchikey:
        # FR-015: user submitted a structure whose InChIKey doesn't match the
        # expected InChIKey.  Leave the item as NEED_STRUCTURE (retryable) and
        # return a 422 error — do NOT permanently strand it as RESULT_KEY_MISMATCH
        # (that status is reserved for remote-response mismatches, FR-018).
        return JsonResponse(
            {
                "error": (
                    f"Submitted InChIKey {norm.inchikey} does not match "
                    f"expected {item.inchikey}"
                ),
                "submitted_inchikey": norm.inchikey,
                "expected_inchikey": item.inchikey,
            },
            status=422,
        )

    resolved_inchikey = norm.inchikey
    item.inchikey = resolved_inchikey

    result = lookup_current_classification(resolved_inchikey)
    if result is not None:
        item.status = (
            BatchItemStatus.FOUND_LIVE_CACHE
            if result.tier == LookupTier.LIVE
            else BatchItemStatus.FOUND_SEED_CACHE
        )
        item.save(update_fields=["status", "inchikey"])
    elif job.remote_enabled:
        from apps.batches.services import _get_or_create_compound, _get_or_create_remote_task

        compound = _get_or_create_compound(
            resolved_inchikey, structure, norm.canonical_smiles, norm.inchi
        )
        item.compound = compound
        item.status = BatchItemStatus.PENDING_CLASSIFICATION
        item.save(update_fields=["status", "inchikey", "compound"])
        _get_or_create_remote_task(resolved_inchikey, compound)
    else:
        item.message = "Structure provided; remote classification disabled."
        item.save(update_fields=["inchikey", "message"])

    # Recompute job counters after every structure resubmit — critical for
    # cache-hit paths (FOUND_*) where no remote fan-out ever fires.
    recompute_job_counters(job.pk)

    return JsonResponse({"status": item.status})


@csrf_exempt  # CSRF N/A: token-only; require_shared_access provides compensating control
@require_shared_access
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
        token, expires_at = create_export(job, fmt, secret)
    except PermissionError:
        return JsonResponse({"error": "Invalid secret."}, status=403)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    return JsonResponse({"token": token, "expires_at": expires_at}, status=201)


@csrf_exempt  # CSRF N/A: token-only; require_shared_access provides compensating control
@require_shared_access
@require_http_methods(["GET"])
def download_export_view(request: HttpRequest, token: str) -> FileResponse | HttpResponseNotFound:
    """GET /api/exports/{token} — download an export by token."""
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

    response = FileResponse(open(path, "rb"), content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{path.name}"'
    return response
