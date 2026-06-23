"""Web UI views for batch jobs (Task 8).

Routes
------
GET  /                              — Submit page
POST /                              — Process submission, redirect to job page
GET  /jobs/{uuid}/{secret}/         — Job detail page
GET  /jobs/{uuid}/{secret}/items    — HTMX partial: items table
GET  /items/{id}/{secret}/structure — Structure-completion form
POST /items/{id}/{secret}/structure — Process structure submission
GET  /stats/                        — DB status page
"""
from __future__ import annotations

from django.contrib.auth.hashers import check_password
from django.core.paginator import Paginator
from django.db.models import Count, Q, QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render

from apps.batches.ingest import parse_text_input
from apps.batches.models import BatchItem, BatchJob
from apps.batches.services import _get_or_create_remote_task, create_job
from apps.classifications.services import lookup_current_classification
from apps.compounds.models import CompoundStructure
from apps.compounds.validators import normalize_row
from apps.core.enums import BatchItemStatus, BatchJobStatus

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


# ---------------------------------------------------------------------------
# Submit page
# ---------------------------------------------------------------------------


def submit_view(request: HttpRequest) -> HttpResponse:
    """GET / — Show submit form. POST / — Process submission."""
    if request.method == "POST":
        text_input: str = request.POST.get("text_input", "")
        remote_enabled_str = request.POST.get("remote_enabled", "")
        remote_enabled = remote_enabled_str.lower() in ("on", "true", "1", "yes")

        rows = list(parse_text_input(text_input))

        # Handle file upload
        file = request.FILES.get("input_file")
        if file:
            file_text = file.read().decode("utf-8", errors="replace")
            file_rows = list(parse_text_input(file_text))
            # Re-index to continue after text_input rows
            offset = len(rows)
            for row in file_rows:
                rows.append({**row, "row_index": row["row_index"] + offset})

        job, plaintext_secret = create_job(rows, remote_enabled=remote_enabled)
        return redirect(f"/jobs/{job.pk}/{plaintext_secret}/")

    return render(request, "batches/submit.html")


# ---------------------------------------------------------------------------
# Job detail page
# ---------------------------------------------------------------------------


def job_detail_view(request: HttpRequest, job_id: str, secret: str) -> HttpResponse:
    """GET /jobs/{uuid}/{secret}/ — Job detail with paginated results table."""
    job = get_object_or_404(BatchJob, pk=job_id)
    if not check_password(secret, job.secret_hash):
        return HttpResponseForbidden("Invalid secret.")

    status_filter = request.GET.get("status_filter", "")
    search = request.GET.get("search", "")
    page_number = request.GET.get("page", 1)

    items_qs = _filter_items(job, status_filter, search)
    paginator = Paginator(items_qs, PAGE_SIZE)
    page_obj = paginator.get_page(page_number)

    job_complete = job.status == BatchJobStatus.COMPLETE

    return render(
        request,
        "batches/job_detail.html",
        {
            "job": job,
            "secret": secret,
            "items": page_obj,
            "page_obj": page_obj,
            "status_filter": status_filter,
            "search": search,
            "job_complete": job_complete,
        },
    )


def item_table_partial_view(request: HttpRequest, job_id: str, secret: str) -> HttpResponse:
    """GET /jobs/{uuid}/{secret}/items — HTMX partial returning only the items table."""
    job = get_object_or_404(BatchJob, pk=job_id)
    if not check_password(secret, job.secret_hash):
        return HttpResponseForbidden("Invalid secret.")

    # Refresh job from DB before rendering
    job.refresh_from_db()

    status_filter = request.GET.get("status_filter", "")
    search = request.GET.get("search", "")
    page_number = request.GET.get("page", 1)

    items_qs = _filter_items(job, status_filter, search)
    paginator = Paginator(items_qs, PAGE_SIZE)
    page_obj = paginator.get_page(page_number)

    job_complete = job.status == BatchJobStatus.COMPLETE

    return render(
        request,
        "batches/item_table.html",
        {
            "job": job,
            "secret": secret,
            "items": page_obj,
            "page_obj": page_obj,
            "status_filter": status_filter,
            "search": search,
            "job_complete": job_complete,
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


def structure_form_view(request: HttpRequest, item_id: int, secret: str) -> HttpResponse:
    """GET/POST /items/{id}/{secret}/structure — Submit a structure for a NEED_STRUCTURE item."""
    item = get_object_or_404(BatchItem, pk=item_id)
    job = item.job

    if not check_password(secret, job.secret_hash):
        return HttpResponseForbidden("Invalid secret.")

    error: str | None = None
    submitted_structure: str = ""

    if request.method == "POST":
        submitted_structure = request.POST.get("structure", "").strip()

        if not submitted_structure:
            error = "Please enter a structure (SMILES or InChI)."
        else:
            # FR-015 Key-match guard
            norm = normalize_row(submitted_structure)

            if norm.error or norm.inchikey is None:
                error = f"Could not parse structure: {norm.error or 'unknown error'}"
            elif item.inchikey and norm.inchikey != item.inchikey:
                # Mismatch — mark and show error
                error = (
                    f"RESULT_KEY_MISMATCH: submitted structure InChIKey "
                    f"({norm.inchikey}) does not match expected InChIKey "
                    f"({item.inchikey})."
                )
                # Update item status to RESULT_KEY_MISMATCH but don't save yet
                # (spec says "show error, item status unchanged" per test)
            else:
                # Keys match (or item has no prior inchikey) — run classification lookup
                resolved_inchikey = norm.inchikey
                item.inchikey = resolved_inchikey

                result = lookup_current_classification(resolved_inchikey)
                if result is not None:
                    from apps.classifications.services import LookupTier

                    item.status = (
                        BatchItemStatus.FOUND_LIVE_CACHE
                        if result.tier == LookupTier.LIVE
                        else BatchItemStatus.FOUND_SEED_CACHE
                    )
                    item.save(update_fields=["status", "inchikey"])
                elif job.remote_enabled:
                    # Ensure compound exists
                    from apps.batches.services import _get_or_create_compound

                    compound = _get_or_create_compound(
                        resolved_inchikey,
                        submitted_structure,
                        norm.canonical_smiles,
                        norm.inchi,
                    )
                    item.compound = compound
                    item.status = BatchItemStatus.PENDING_CLASSIFICATION
                    item.save(update_fields=["status", "inchikey", "compound"])
                    _get_or_create_remote_task(resolved_inchikey, compound)
                else:
                    item.message = "Structure provided but no remote classification available."
                    item.save(update_fields=["inchikey", "message"])

                return redirect(f"/jobs/{job.pk}/{secret}/")

    return render(
        request,
        "batches/structure_form.html",
        {
            "item": item,
            "secret": secret,
            "error": error,
            "submitted_structure": submitted_structure,
        },
    )


# ---------------------------------------------------------------------------
# Stats page
# ---------------------------------------------------------------------------


def stats_view(request: HttpRequest) -> HttpResponse:
    """GET /stats/ — DB status page with aggregate counts."""
    jobs_qs = BatchJob.objects.aggregate(
        total=Count("pk"),
        complete=Count("pk", filter=Q(status=BatchJobStatus.COMPLETE)),
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
