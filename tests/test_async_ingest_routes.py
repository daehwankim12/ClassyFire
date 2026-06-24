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
from apps.core.enums import BatchItemStatus, BatchProcessingState


@pytest.mark.django_db
def test_api_create_job_stages_without_processing(client, monkeypatch, settings):
    settings.INGEST_DEFER_HEAVY_PROCESSING = True

    def fail_if_called(job):
        raise AssertionError("HTTP route must not process job items inline")

    monkeypatch.setattr("apps.batches.services.process_job_items", fail_if_called)

    response = client.post(
        "/api/jobs",
        data={"text_input": "CCO\n", "remote_enabled": False},
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer dev-token",
    )

    assert response.status_code == 202
    job = BatchJob.objects.get(pk=response.json()["job_id"])
    assert job.processing_state == BatchProcessingState.PARSING
    assert list(job.items.values_list("status", flat=True)) == [
        BatchItemStatus.PENDING_NORMALIZATION
    ]


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
