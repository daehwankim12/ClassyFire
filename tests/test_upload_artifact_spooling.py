from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.batches.models import BatchItem, BatchJob
from apps.core.enums import BatchProcessingState


@pytest.mark.django_db
def test_api_upload_spools_artifact_without_creating_items(client, settings, monkeypatch, tmp_path):
    settings.INGEST_DEFER_HEAVY_PROCESSING = True
    settings.UPLOAD_ARTIFACT_DIR = str(tmp_path / "uploads")

    def forbidden_create_job_records_streaming(*args, **kwargs):
        raise AssertionError("HTTP route must not parse rows or create BatchItems")

    monkeypatch.setattr(
        "apps.core.security.create_job_records_streaming",
        forbidden_create_job_records_streaming,
        raising=False,
    )

    upload = SimpleUploadedFile(
        "input.csv",
        b"id,smiles\nrow1,CCO\n",
        content_type="text/csv",
    )
    response = client.post(
        "/api/jobs",
        data={"input_file": upload, "remote_enabled": "true"},
        HTTP_AUTHORIZATION="Bearer dev-token",
    )

    assert response.status_code == 202
    job = BatchJob.objects.get()
    assert job.processing_state == BatchProcessingState.UPLOADING
    assert BatchItem.objects.count() == 0
    assert job.upload_artifact_path
    assert (tmp_path / "uploads" / job.upload_artifact_path).exists()
    assert not list((tmp_path / "uploads").glob("*.tmp"))
