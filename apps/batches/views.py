"""Web UI views for batch jobs (Task 8).

Routes
------
GET  /                              — Submit page
POST /                              — Process submission, redirect to job page
GET  /jobs/{uuid}/{secret}/         — Legacy link exchange, then redirect
GET  /jobs/{uuid}/{secret}/items    — Legacy link exchange, then redirect
GET  /items/{id}/{secret}/structure — Legacy link exchange, then redirect
POST /items/{id}/{secret}/structure — Process success or redirect errors
GET  /stats/                        — DB status page
"""
from __future__ import annotations

import json

from django.contrib.auth.hashers import check_password
from django.core.paginator import Paginator
from django.db.models import Count, Q, QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render

from apps.batches.ingest import parse_text_input
from apps.batches.models import BatchItem, BatchJob
from apps.batches.services import (
    StructureKeyMismatchError,
    StructureSubmissionConflictError,
    StructureSubmissionError,
    submit_structure_for_item,
)
from apps.batches.upload import iter_combined_rows
from apps.compounds.models import CompoundStructure
from apps.core.enums import BatchItemStatus, BatchJobStatus
from apps.core.security import (
    UploadTooLargeError,
    check_upload_size,
    request_has_job_access_cookie,
    require_shared_access_web,
    set_job_access_cookie,
    stage_job_under_quota,
)

PAGE_SIZE = 100

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

TERMINAL_JOB_STATUSES = (
    BatchJobStatus.COMPLETE,
    BatchJobStatus.COMPLETE_WITH_ACTION_REQUIRED,
)


# ---------------------------------------------------------------------------
# Submit page
# ---------------------------------------------------------------------------


@require_shared_access_web
def submit_view(request: HttpRequest) -> HttpResponse:
    """GET / — Show submit form. POST / — Process submission."""
    if request.method == "POST":
        text_input: str = request.POST.get("text_input", "")
        remote_enabled_str = request.POST.get("remote_enabled", "")
        remote_enabled = remote_enabled_str.lower() in ("on", "true", "1", "yes")

        # Handle file upload — BOUNDED STREAMING parse (F-01). The uploaded file
        # is dispatched on suffix+content-type and parsed record-by-record via
        # iter_combined_rows; we NEVER file.read() the whole upload.
        file = request.FILES.get("input_file")
        if file:
            # Reject oversize uploads before reading content.
            # file.size may be None when the upload size is unknown; in that
            # case we fall through and let the read proceed (no size to check).
            try:
                check_upload_size(file.size if file.size is not None else 0)
            except UploadTooLargeError as exc:
                return render(
                    request,
                    "batches/submit.html",
                    {"error": str(exc)},
                    status=413,
                )

        # Lazily chain text-input + file rows, re-indexed continuously and with
        # provenance (external_id, raw input) preserved.
        row_iter = iter_combined_rows(
            parse_text_input(text_input),
            file,
            filename=file.name if file else None,
            content_type=getattr(file, "content_type", None) if file else None,
        )

        # DEFERRED mode (F-11, always on): STAGE items only and RETURN
        # immediately; the durable `process_jobs` worker drains the job. The
        # row iterator is consumed straight into the staging insert — no full
        # list materialisation in the HTTP request.
        job, plaintext_secret, quota_error = stage_job_under_quota(
            request,
            row_iter,
            remote_enabled=remote_enabled,
        )
        if quota_error is not None:
            quota_payload = json.loads(quota_error.content.decode("utf-8"))
            return render(
                request,
                "batches/submit.html",
                {"error": quota_payload["error"]},
                status=quota_error.status_code,
            )

        if job is None:
            return render(
                request,
                "batches/submit.html",
                {"error": "Job creation failed."},
                status=500,
            )
        return redirect(f"/jobs/{job.pk}/claim/{plaintext_secret}/")

    return render(request, "batches/submit.html")


# ---------------------------------------------------------------------------
# Job detail page
# ---------------------------------------------------------------------------


def _has_job_access(request: HttpRequest, job: BatchJob, secret: str | None) -> bool:
    if secret is None:
        return request_has_job_access_cookie(request, job.pk)
    return bool(check_password(secret, job.secret_hash))


def _is_terminal_job(job: BatchJob) -> bool:
    return job.status in TERMINAL_JOB_STATUSES


def _blocking_pending_count(job: BatchJob) -> int:
    pending_normalization = BatchItem.objects.filter(
        job=job,
        status=BatchItemStatus.PENDING_NORMALIZATION,
    ).count()
    return job.remote_pending_count + pending_normalization


def job_claim_view(request: HttpRequest, job_id: str, secret: str) -> HttpResponse:
    """Exchange a one-time URL secret for a secretless job-access cookie."""
    job = get_object_or_404(BatchJob, pk=job_id)
    if not check_password(secret, job.secret_hash):
        return HttpResponseForbidden("Invalid secret.")

    response = HttpResponse(status=303)
    response["Location"] = f"/jobs/{job.pk}/"
    set_job_access_cookie(response, request, job.pk)
    return response


def job_detail_view(
    request: HttpRequest,
    job_id: str,
    secret: str | None = None,
) -> HttpResponse:
    """GET /jobs/{uuid}/ or /jobs/{uuid}/{secret}/ — Job detail page."""
    job = get_object_or_404(BatchJob, pk=job_id)
    if secret is not None:
        return _exchange_legacy_job_secret(request, job, secret)

    if not _has_job_access(request, job, secret):
        return HttpResponseForbidden("Invalid secret.")

    status_filter = request.GET.get("status_filter", "")
    search = request.GET.get("search", "")
    page_number = request.GET.get("page", 1)

    items_qs = _filter_items(job, status_filter, search)
    paginator = Paginator(items_qs, PAGE_SIZE)
    page_obj = paginator.get_page(page_number)

    job_complete = _is_terminal_job(job)
    blocking_pending_count = _blocking_pending_count(job)

    return render(
        request,
        "batches/job_detail.html",
        {
            "job": job,
            "secret": secret,
            "secretless": secret is None,
            "items": page_obj,
            "page_obj": page_obj,
            "status_filter": status_filter,
            "search": search,
            "job_complete": job_complete,
            "blocking_pending_count": blocking_pending_count,
        },
    )


def _exchange_legacy_job_secret(
    request: HttpRequest,
    job: BatchJob,
    secret: str,
) -> HttpResponse:
    """Exchange legacy secret-bearing job URLs without rendering the secret."""
    if not check_password(secret, job.secret_hash):
        return HttpResponseForbidden("Invalid secret.")

    response = HttpResponse(status=303)
    response["Location"] = f"/jobs/{job.pk}/"
    set_job_access_cookie(response, request, job.pk)
    return response


def _redirect_with_job_access_cookie(
    request: HttpRequest,
    job: BatchJob,
    location: str,
    *,
    status: int = 303,
) -> HttpResponse:
    response = HttpResponse(status=status)
    response["Location"] = location
    set_job_access_cookie(response, request, job.pk)
    return response


def _append_query_string(request: HttpRequest, location: str) -> str:
    query_string = request.META.get("QUERY_STRING", "")
    if not query_string:
        return location
    return f"{location}?{query_string}"


def item_table_partial_view(
    request: HttpRequest,
    job_id: str,
    secret: str | None = None,
) -> HttpResponse:
    """GET /jobs/{uuid}/items or /jobs/{uuid}/{secret}/items — HTMX partial."""
    job = get_object_or_404(BatchJob, pk=job_id)
    if secret is not None:
        if not check_password(secret, job.secret_hash):
            return HttpResponseForbidden("Invalid secret.")
        return _redirect_with_job_access_cookie(
            request,
            job,
            _append_query_string(request, f"/jobs/{job.pk}/items"),
        )

    if not _has_job_access(request, job, secret):
        return HttpResponseForbidden("Invalid secret.")

    # Refresh job from DB before rendering
    job.refresh_from_db()

    status_filter = request.GET.get("status_filter", "")
    search = request.GET.get("search", "")
    page_number = request.GET.get("page", 1)

    items_qs = _filter_items(job, status_filter, search)
    paginator = Paginator(items_qs, PAGE_SIZE)
    page_obj = paginator.get_page(page_number)

    job_complete = _is_terminal_job(job)
    blocking_pending_count = _blocking_pending_count(job)

    return render(
        request,
        "batches/item_table.html",
        {
            "job": job,
            "secret": secret,
            "secretless": secret is None,
            "items": page_obj,
            "page_obj": page_obj,
            "status_filter": status_filter,
            "search": search,
            "job_complete": job_complete,
            "blocking_pending_count": blocking_pending_count,
        },
    )


def _filter_items(job: BatchJob, status_filter: str, search: str) -> QuerySet:
    """Build a filtered+searched queryset of BatchItems for a job."""
    qs = BatchItem.objects.filter(job=job).order_by("row_order")

    if status_filter and status_filter in STATUS_FILTER_MAP:
        qs = qs.filter(status__in=STATUS_FILTER_MAP[status_filter])

    if search:
        qs = qs.filter(
            Q(original_input__icontains=search) | Q(external_id__icontains=search)
        )

    return qs


# ---------------------------------------------------------------------------
# Structure completion
# ---------------------------------------------------------------------------


def structure_form_view(
    request: HttpRequest,
    item_id: int,
    secret: str | None = None,
) -> HttpResponse:
    """GET/POST structure form, authorized by job cookie or legacy secret path."""
    item = get_object_or_404(BatchItem, pk=item_id)
    job = item.job
    legacy_secret = secret

    if legacy_secret is not None:
        if not check_password(legacy_secret, job.secret_hash):
            return HttpResponseForbidden("Invalid secret.")
        if request.method == "GET":
            return _redirect_with_job_access_cookie(
                request,
                job,
                f"/items/{item.pk}/structure",
            )
        secret = None
    elif not _has_job_access(request, job, secret):
        return HttpResponseForbidden("Invalid secret.")

    error: str | None = None
    submitted_structure: str = ""

    if request.method == "POST":
        submitted_structure = request.POST.get("structure", "").strip()

        if not submitted_structure:
            error = "Please enter a structure (SMILES or InChI)."
        else:
            try:
                submit_structure_for_item(item.pk, submitted_structure)
            except StructureKeyMismatchError as exc:
                # Mismatch remains retryable: leave the item unchanged and show
                # an error so the user can submit a matching structure.
                error = (
                    f"Structure mismatch: submitted InChIKey "
                    f"({exc.submitted_inchikey}) does not match expected InChIKey "
                    f"({exc.expected_inchikey})."
                )
            except StructureSubmissionConflictError as exc:
                return HttpResponse(str(exc), status=409)
            except StructureSubmissionError as exc:
                error = str(exc)
            else:
                response: HttpResponse = redirect(f"/jobs/{job.pk}/")
                if legacy_secret is not None:
                    set_job_access_cookie(response, request, job.pk)
                return response

    if legacy_secret is not None:
        return _redirect_with_job_access_cookie(
            request,
            job,
            f"/items/{item.pk}/structure",
        )

    return render(
        request,
        "batches/structure_form.html",
        {
            "item": item,
            "secret": secret,
            "secretless": secret is None,
            "error": error,
            "submitted_structure": submitted_structure,
        },
    )


# ---------------------------------------------------------------------------
# Stats page
# ---------------------------------------------------------------------------


@require_shared_access_web
def stats_view(request: HttpRequest) -> HttpResponse:
    """GET /stats/ — DB status page with aggregate counts."""
    jobs_qs = BatchJob.objects.aggregate(
        total=Count("pk"),
        complete=Count("pk", filter=Q(status__in=TERMINAL_JOB_STATUSES)),
        in_progress=Count("pk", filter=Q(status=BatchJobStatus.IN_PROGRESS)),
        pending=Count("pk", filter=Q(status=BatchJobStatus.PENDING)),
        failed=Count("pk", filter=Q(status=BatchJobStatus.FAILED)),
    )

    item_counts: dict[str, int] = {}
    for status_value, _label in BatchItemStatus.choices:
        count = BatchItem.objects.filter(status=status_value).count()
        if count:
            item_counts[status_value] = count

    compound_total = CompoundStructure.objects.count()

    return render(
        request,
        "batches/stats.html",
        {
            "jobs_total": jobs_qs["total"],
            "jobs_complete": jobs_qs["complete"],
            "jobs_in_progress": jobs_qs["in_progress"],
            "jobs_pending": jobs_qs["pending"],
            "jobs_failed": jobs_qs["failed"],
            "item_counts": item_counts,
            "compound_total": compound_total,
        },
    )
