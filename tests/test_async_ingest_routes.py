"""Gate 4 / Task 1: Route-level async ingest tests.

Verifies that ALL upload routes (HTML + JSON API) go through the async
ingest path: the HTTP request only authenticates, stream-parses, stages
BatchItem rows (PENDING_NORMALIZATION), and returns 202.  Heavy
normalization/lookup/upsert/remote-task creation happens only in the
``process_jobs`` worker.
"""
from __future__ import annotations

import io

import pytest

from apps.batches.models import BatchJob
from apps.core.enums import BatchProcessingState


@pytest.mark.django_db
def test_api_create_job_stages_without_processing(client, monkeypatch, settings, tmp_path):
    # H-04: a TEXT submit now spools to a durable artifact and returns 202 in
    # UPLOADING with ZERO BatchItems in-request (parsing/row creation is deferred
    # to the worker, exactly like a file upload). The request must NOT parse.
    settings.INGEST_DEFER_HEAVY_PROCESSING = True
    settings.UPLOAD_ARTIFACT_DIR = str(tmp_path / "uploads")

    def fail_if_parsed(*args, **kwargs):
        raise AssertionError("HTTP route must not parse rows or create BatchItems")

    monkeypatch.setattr(
        "apps.core.security.create_job_records_streaming",
        fail_if_parsed,
        raising=False,
    )

    response = client.post(
        "/api/jobs",
        data={"text_input": "CCO\n", "remote_enabled": False},
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer dev-token",
    )

    assert response.status_code == 202
    job = BatchJob.objects.get(pk=response.json()["job_id"])
    assert job.processing_state == BatchProcessingState.UPLOADING
    assert job.items.count() == 0
    assert job.upload_artifact_path

    # Driving the worker stages the spooled text into rows and advances state.
    from apps.batches.services import run_pending_jobs

    run_pending_jobs("test-worker", max_jobs=1)

    job.refresh_from_db()
    assert job.processing_state != BatchProcessingState.UPLOADING
    assert job.items.count() == 1


@pytest.mark.django_db
def test_html_submit_stages_without_processing(client, monkeypatch, settings, tmp_path):
    # H-04: same deferred contract for the HTML submit route.
    settings.INGEST_DEFER_HEAVY_PROCESSING = True
    settings.UPLOAD_ARTIFACT_DIR = str(tmp_path / "uploads")

    def fail_if_parsed(*args, **kwargs):
        raise AssertionError("HTML route must not parse rows or create BatchItems")

    monkeypatch.setattr(
        "apps.core.security.create_job_records_streaming",
        fail_if_parsed,
        raising=False,
    )

    response = client.post(
        "/",
        data={"text_input": "CCO\n", "remote_enabled": "on"},
    )

    assert response.status_code == 302
    location = response["Location"]
    # Location is /jobs/<uuid>/claim/<secret>/
    assert location.startswith("/jobs/")
    parts = location.strip("/").split("/")
    assert parts[2] == "claim", f"Unexpected redirect: {location}"
    job_id = parts[1]

    job = BatchJob.objects.get(pk=job_id)
    assert job.processing_state == BatchProcessingState.UPLOADING
    assert job.items.count() == 0
    assert job.upload_artifact_path

    from apps.batches.services import run_pending_jobs

    run_pending_jobs("test-worker", max_jobs=1)

    job.refresh_from_db()
    assert job.processing_state != BatchProcessingState.UPLOADING
    assert job.items.count() == 1


@pytest.mark.django_db
def test_api_multipart_upload_does_not_materialize_rows(client, monkeypatch, settings):
    settings.INGEST_DEFER_HEAVY_PROCESSING = True

    class ExplodingList(list):
        def __init__(self, *args, **kwargs):
            raise AssertionError("route must not call list(row_iter)")

    monkeypatch.setattr("apps.batches.api.list", ExplodingList, raising=False)

    upload = io.BytesIO(b"id,smiles\nrow1,CCO\n")
    upload.name = "input.csv"
    response = client.post(
        "/api/jobs",
        data={"input_file": upload, "remote_enabled": "false"},
        HTTP_AUTHORIZATION="Bearer dev-token",
    )

    assert response.status_code == 202
