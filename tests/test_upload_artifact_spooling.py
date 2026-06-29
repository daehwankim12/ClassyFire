from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.batches.models import BatchItem, BatchJob
from apps.core.enums import BatchJobStatus, BatchProcessingState
from apps.operations.cleanup import cleanup_expired


@pytest.mark.django_db(transaction=True)
def test_spooled_upload_removed_when_job_create_rolls_back(
    settings, tmp_path, monkeypatch, rf
):
    """A failure AFTER the spool must not orphan the artifact on disk (R-07)."""
    settings.UPLOAD_ARTIFACT_DIR = str(tmp_path / "uploads")
    from apps.core.security import spool_upload_under_quota

    request = rf.post("/api/jobs")
    upload = SimpleUploadedFile("input.csv", b"C\n", content_type="text/csv")

    def fail_create(*args, **kwargs):
        raise RuntimeError("db create failed after spool")

    monkeypatch.setattr(BatchJob.objects, "create", fail_create)

    with pytest.raises(RuntimeError, match="db create failed"):
        spool_upload_under_quota(request, upload, remote_enabled=True)

    assert list((tmp_path / "uploads").glob("*")) == []


@pytest.mark.django_db
def test_cleanup_expired_deletes_final_upload_artifact_after_job_delete(
    settings, tmp_path
):
    """Deleting an expired job also unlinks its linked final upload artifact."""
    settings.UPLOAD_ARTIFACT_DIR = str(tmp_path / "uploads")
    settings.JOB_RETENTION_DAYS = 1
    artifact_dir = Path(settings.UPLOAD_ARTIFACT_DIR)
    artifact_dir.mkdir()
    artifact = artifact_dir / "raw.csv"
    artifact.write_text("C\n")
    job = BatchJob.objects.create(
        secret_hash="x",
        status=BatchJobStatus.COMPLETE,
        completed_at=timezone.now() - timedelta(days=2),
        upload_artifact_path=artifact.name,
    )

    result = cleanup_expired(actor="test")

    assert result.jobs_deleted == 1
    assert not artifact.exists()
    assert not BatchJob.objects.filter(pk=job.pk).exists()


@pytest.mark.django_db
def test_cleanup_expired_deletes_final_upload_artifact_after_default_seven_days(
    settings, tmp_path
):
    """Completed job pages and linked upload artifacts expire after 7 days by default."""
    settings.UPLOAD_ARTIFACT_DIR = str(tmp_path / "uploads")
    artifact_dir = Path(settings.UPLOAD_ARTIFACT_DIR)
    artifact_dir.mkdir()
    artifact = artifact_dir / "raw.csv"
    artifact.write_text("C\n")
    job = BatchJob.objects.create(
        secret_hash="x",
        status=BatchJobStatus.COMPLETE,
        completed_at=timezone.now() - timedelta(days=8),
        upload_artifact_path=artifact.name,
    )

    result = cleanup_expired(actor="test")

    assert result.jobs_deleted == 1
    assert result.upload_artifacts_deleted == 1
    assert not artifact.exists()
    assert not BatchJob.objects.filter(pk=job.pk).exists()


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
