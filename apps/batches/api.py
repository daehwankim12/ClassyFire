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

Structured for Task 8 extension:
    Task 8 will add GET /api/jobs/{id}, /api/jobs/{id}/items,
    /api/jobs/{id}/export.  Add those views here.
"""
from __future__ import annotations

import json

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.batches.ingest import parse_text_input
from apps.batches.services import create_job


@csrf_exempt
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
    # For this task, only text_input (plain newline-separated) is supported.
    # File uploads will be added in Task 8.
    text_input: str = body.get("text_input") or ""
    rows = list(parse_text_input(text_input))

    # Create the job and run local processing
    job, plaintext_secret = create_job(rows, remote_enabled=remote_enabled)

    return JsonResponse(
        {"job_id": str(job.pk), "result_secret": plaintext_secret},
        status=201,
    )
