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
    # The terminal job's raw upload is released by the pre-retention sweep (or,
    # failing that, unlinked with the job); either way it is gone exactly once.
    assert result.upload_artifacts_released + result.upload_artifacts_deleted == 1
    assert not artifact.exists()
    assert not BatchJob.objects.filter(pk=job.pk).exists()


@pytest.mark.django_db
def test_cleanup_keeps_byte_reservation_when_artifact_unlink_fails(
    settings, tmp_path, monkeypatch
):
    """A failed artifact deletion keeps that job's reservation, not the whole run."""
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
        upload_size_bytes=artifact.stat().st_size,
    )
    original_unlink = Path.unlink

    def fail_artifact_unlink(path, *args, **kwargs):
        if path == artifact:
            raise OSError("simulated upload volume failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_artifact_unlink)

    other_artifact = artifact_dir / "other.csv"
    other_artifact.write_text("CC\n")
    other_job = BatchJob.objects.create(
        secret_hash="y",
        status=BatchJobStatus.COMPLETE,
        completed_at=timezone.now() - timedelta(days=2),
        upload_artifact_path=other_artifact.name,
        upload_size_bytes=other_artifact.stat().st_size,
    )

    result = cleanup_expired(actor="test")

    # The unremovable artifact keeps its job and byte reservation for a retry…
    job.refresh_from_db()
    assert job.upload_artifact_path == artifact.name
    assert job.upload_size_bytes == artifact.stat().st_size
    assert result.jobs_kept_for_unlink_failure == 1
    # …while retention still proceeds for every other expired job.
    assert not BatchJob.objects.filter(pk=other_job.pk).exists()
    assert not other_artifact.exists()
    assert result.jobs_deleted == 1


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


@pytest.mark.django_db(transaction=True)
def test_retained_byte_quota_counts_uploads_independently_of_rows(
    settings, tmp_path, rf
):
    """A second retained upload is rejected even before either file is parsed."""
    from apps.core.security import spool_upload_under_quota

    settings.UPLOAD_ARTIFACT_DIR = str(tmp_path / "uploads")
    settings.UPLOAD_RETAINED_BYTES_PER_PRINCIPAL = 3
    settings.UPLOAD_RETAINED_BYTES_GLOBAL = 1_000
    settings.JOB_CREATE_RATE_LIMIT_PER_MINUTE = -1
    settings.ACTIVE_JOB_CAP = 20
    settings.DISK_UPLOAD_PAUSE_PERCENT = 100
    request = rf.post("/api/jobs", REMOTE_ADDR="198.51.100.10")

    first = SimpleUploadedFile("first.txt", b"C\n", content_type="text/plain")
    first_job, _secret, first_error = spool_upload_under_quota(
        request, first, remote_enabled=False
    )
    assert first_error is None
    assert first_job is not None

    second = SimpleUploadedFile("second.txt", b"C\n", content_type="text/plain")
    second_job, _secret, second_error = spool_upload_under_quota(
        request, second, remote_enabled=False
    )
    assert second_job is None
    assert second_error is not None
    assert second_error.status_code == 429
    assert BatchJob.objects.count() == 1
    assert sum(path.stat().st_size for path in (tmp_path / "uploads").iterdir()) == 2


@pytest.mark.django_db(transaction=True)
def test_global_retained_byte_quota_counts_upload_directory_orphans(
    settings, tmp_path, rf
):
    """Unowned retained files consume global capacity and prevent another spool."""
    from apps.core.security import spool_upload_under_quota

    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    (upload_root / "orphan.bin").write_bytes(b"1234")
    settings.UPLOAD_ARTIFACT_DIR = str(upload_root)
    settings.UPLOAD_RETAINED_BYTES_PER_PRINCIPAL = 1_000
    settings.UPLOAD_RETAINED_BYTES_GLOBAL = 5
    settings.JOB_CREATE_RATE_LIMIT_PER_MINUTE = -1
    settings.ACTIVE_JOB_CAP = 20
    settings.DISK_UPLOAD_PAUSE_PERCENT = 100
    request = rf.post("/api/jobs", REMOTE_ADDR="198.51.100.11")

    upload = SimpleUploadedFile("input.txt", b"CC\n", content_type="text/plain")
    job, _secret, error = spool_upload_under_quota(
        request, upload, remote_enabled=False
    )

    assert job is None
    assert error is not None
    assert error.status_code == 503
    assert list(upload_root.iterdir()) == [upload_root / "orphan.bin"]


@pytest.mark.django_db
def test_disk_guard_uses_upload_artifact_filesystem(settings, tmp_path, monkeypatch, rf):
    """The admission high-water check resolves and measures the upload root."""
    from collections import namedtuple

    from apps.core.security import validate_job_create_controls

    upload_root = tmp_path / "separate-upload-volume"
    settings.UPLOAD_ARTIFACT_DIR = str(upload_root)
    settings.DISK_UPLOAD_PAUSE_PERCENT = 85
    measured = []
    usage = namedtuple("usage", "total used free")(100, 10, 90)

    def fake_disk_usage(path):
        measured.append(Path(path))
        return usage

    monkeypatch.setattr("apps.core.security.shutil.disk_usage", fake_disk_usage)
    request = rf.post("/api/jobs", REMOTE_ADDR="198.51.100.12")

    assert validate_job_create_controls(request, 0) is None
    assert measured == [upload_root.resolve()]


@pytest.mark.django_db(transaction=True)
def test_disk_guard_leaves_headroom_for_streamed_upload(
    settings, tmp_path, monkeypatch, rf
):
    """Streaming stops before an admitted upload crosses the upload-volume threshold."""
    from collections import namedtuple

    from apps.core.security import spool_upload_under_quota

    upload_root = tmp_path / "separate-upload-volume"
    settings.UPLOAD_ARTIFACT_DIR = str(upload_root)
    settings.UPLOAD_RETAINED_BYTES_PER_PRINCIPAL = 1_000
    settings.UPLOAD_RETAINED_BYTES_GLOBAL = 1_000
    settings.JOB_CREATE_RATE_LIMIT_PER_MINUTE = -1
    settings.ACTIVE_JOB_CAP = 20
    settings.DISK_UPLOAD_PAUSE_PERCENT = 85
    usage = namedtuple("usage", "total used free")(100, 80, 20)
    monkeypatch.setattr("apps.core.security.shutil.disk_usage", lambda _path: usage)
    request = rf.post("/api/jobs", REMOTE_ADDR="198.51.100.13")
    upload = SimpleUploadedFile("input.txt", b"123456", content_type="text/plain")

    job, _secret, error = spool_upload_under_quota(
        request, upload, remote_enabled=False
    )

    assert job is None
    assert error is not None
    assert error.status_code == 503
    assert list(upload_root.iterdir()) == []


@pytest.mark.django_db
def test_release_unneeded_upload_artifacts_frees_staged_and_terminal_jobs(
    settings, tmp_path
):
    """Raw uploads of staged or terminal jobs stop counting against byte quotas."""
    from apps.batches.models import BatchItem
    from apps.batches.services import release_unneeded_upload_artifacts
    from apps.core.enums import BatchProcessingState
    from apps.core.security import _principal_retained_upload_bytes

    settings.UPLOAD_ARTIFACT_DIR = str(tmp_path / "uploads")
    artifact_dir = Path(settings.UPLOAD_ARTIFACT_DIR)
    artifact_dir.mkdir()

    def job_with_artifact(name: str, state: str) -> tuple[BatchJob, Path]:
        path = artifact_dir / name
        path.write_text("C\n" * 10)
        job = BatchJob.objects.create(
            secret_hash=name,
            processing_state=state,
            remote_principal="shared",
            upload_artifact_path=path.name,
            upload_size_bytes=path.stat().st_size,
        )
        return job, path

    # Staged before staging released artifacts (e.g. pre-deploy): has items.
    staged, staged_path = job_with_artifact(
        "staged.csv", BatchProcessingState.REMOTE_PENDING
    )
    BatchItem.objects.create(job=staged, row_order=0, original_input="CCO")
    # Rejected before staging (row/daily cap): FAILED, never re-claimed.
    failed, failed_path = job_with_artifact("failed.csv", BatchProcessingState.FAILED)
    # Still waiting to be staged: must keep its raw upload.
    pending, pending_path = job_with_artifact(
        "pending.csv", BatchProcessingState.UPLOADING
    )
    parsing, parsing_path = job_with_artifact(
        "parsing.csv", BatchProcessingState.PARSING
    )

    assert release_unneeded_upload_artifacts() == 2

    for job, path in ((staged, staged_path), (failed, failed_path)):
        job.refresh_from_db()
        assert job.upload_artifact_path is None
        assert job.upload_size_bytes is None
        assert not path.exists()
    for job, path in ((pending, pending_path), (parsing, parsing_path)):
        job.refresh_from_db()
        assert job.upload_artifact_path == path.name
        assert path.exists()
    assert _principal_retained_upload_bytes("shared", 1) == (
        pending_path.stat().st_size + parsing_path.stat().st_size
    )


@pytest.mark.django_db
def test_cleanup_removes_only_old_unreferenced_upload_files(settings, tmp_path):
    """Orphan raw uploads would otherwise shrink the global byte cap forever."""
    import os
    import time

    from apps.core.enums import BatchProcessingState

    settings.UPLOAD_ARTIFACT_DIR = str(tmp_path / "uploads")
    settings.UPLOAD_TEMP_GRACE_SECONDS = 60
    artifact_dir = Path(settings.UPLOAD_ARTIFACT_DIR)
    artifact_dir.mkdir()
    old = time.time() - 3600

    orphan = artifact_dir / "orphan.csv"
    orphan.write_text("C\n")
    os.utime(orphan, (old, old))
    fresh_orphan = artifact_dir / "fresh.csv"
    fresh_orphan.write_text("C\n")
    linked = artifact_dir / "linked.csv"
    linked.write_text("C\n")
    os.utime(linked, (old, old))
    BatchJob.objects.create(
        secret_hash="z",
        processing_state=BatchProcessingState.UPLOADING,
        upload_artifact_path=linked.name,
    )

    result = cleanup_expired(actor="test")

    assert result.upload_orphans_deleted == 1
    assert not orphan.exists()
    assert fresh_orphan.exists()
    assert linked.exists()
