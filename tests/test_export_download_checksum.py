from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.batches.exports import get_export_path
from apps.batches.models import BatchJob, ExportArtifact


@pytest.mark.django_db
def test_download_rejects_corrupt_export_file(tmp_path):
    job = BatchJob.objects.create(secret_hash="hash")
    path = tmp_path / "export.csv"
    path.write_text("good\n")
    artifact = ExportArtifact.objects.create(
        job=job,
        fmt="csv",
        token="token-corrupt-export",
        path=str(path),
        checksum="106675dc1490d5cdd6d1f0410731316ce93fc964c6cf6726e2b0d53e19688feb",
        byte_size=5,
        # SHA-256 of b"[]" — correct signature for an empty job
        rows_signature="4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
        row_count=0,
        job_state_signature="2ca20c6897ba9311712dc13b057ed5a77ec9c5ae650b0e9a12602d69bf992677",
        expires_at=timezone.now() + timedelta(days=1),
    )
    path.write_text("bad\n")
    assert get_export_path(artifact.token) is None


@pytest.mark.django_db
def test_download_rejects_stale_rows_signature_even_when_job_state_matches(tmp_path):
    from apps.batches import exports

    job = BatchJob.objects.create(secret_hash="hash")
    path = tmp_path / "export.csv"
    path.write_text("good\n")
    checksum = exports._checksum_file(path)
    artifact = ExportArtifact.objects.create(
        job=job,
        fmt="csv",
        token="token-stale-rows-export",
        path=str(path),
        checksum=checksum,
        byte_size=path.stat().st_size,
        rows_signature="a" * 64,
        row_count=0,
        job_state_signature=exports._job_state_signature(job),
        expires_at=timezone.now() + timedelta(days=1),
    )

    assert get_export_path(artifact.token) is None


@pytest.mark.django_db
def test_download_does_not_cache_successful_rows_signature_validation(tmp_path, monkeypatch):
    from django.core.cache import cache

    from apps.batches import exports

    cache.clear()
    job = BatchJob.objects.create(secret_hash="hash")
    path = tmp_path / "export.csv"
    path.write_text("good\n")
    rows_signature = exports._compute_rows_signature(job)
    artifact = ExportArtifact.objects.create(
        job=job,
        fmt="csv",
        token="token-valid-export-cache",
        path=str(path),
        checksum=exports._checksum_file(path),
        byte_size=path.stat().st_size,
        rows_signature=rows_signature,
        row_count=0,
        job_state_signature=exports._job_state_signature(job),
        expires_at=timezone.now() + timedelta(days=1),
    )

    calls = 0

    def counting_compute_rows_signature(current_job):
        nonlocal calls
        calls += 1
        assert current_job.pk == job.pk
        return rows_signature

    monkeypatch.setattr(exports, "_compute_rows_signature", counting_compute_rows_signature)

    assert get_export_path(artifact.token) == path
    assert get_export_path(artifact.token) == path
    assert calls == 2


@pytest.mark.django_db
def test_download_rejects_stale_rows_after_successful_validation_cache(tmp_path):
    from django.core.cache import cache

    from apps.batches import exports
    from apps.batches.models import BatchItem

    cache.clear()
    job = BatchJob.objects.create(secret_hash="hash", total_count=1)
    # InChIKeys must satisfy the batch_item_inchikey_format CHECK constraint
    # (^[A-Z]{14}-[A-Z]{10}-[A-Z]$); use two real, distinct keys so changing the
    # row's content below shifts the rows_signature.
    item = BatchItem.objects.create(
        job=job,
        row_order=0,
        inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
        status="FAILED",
    )
    path = tmp_path / "export.csv"
    path.write_text("good\n")
    artifact = ExportArtifact.objects.create(
        job=job,
        fmt="csv",
        token="token-stale-after-valid-cache",
        path=str(path),
        checksum=exports._checksum_file(path),
        byte_size=path.stat().st_size,
        rows_signature=exports._compute_rows_signature(job),
        row_count=1,
        job_state_signature=exports._job_state_signature(job),
        expires_at=timezone.now() + timedelta(days=1),
    )

    assert get_export_path(artifact.token) == path

    item.inchikey = "OKKJLVBELUTLKV-UHFFFAOYSA-N"
    item.save(update_fields=["inchikey"])

    assert exports._job_state_signature(job) == artifact.job_state_signature
    assert exports._compute_rows_signature(job) != artifact.rows_signature
    assert get_export_path(artifact.token) is None
