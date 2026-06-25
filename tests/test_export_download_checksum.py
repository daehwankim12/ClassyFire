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
        expires_at=timezone.now() + timedelta(days=1),
    )
    path.write_text("bad\n")
    assert get_export_path(artifact.token) is None
