"""Tests for Task 2 (Gate 7 M-03): export orphan and stale-temp cleanup.

These tests exercise the orphan-file cleanup phase added to
`apps/operations/cleanup.py`.  Each test:
  - Uses a real Postgres DB (transaction=True for isolation).
  - Points EXPORT_DIR at a pytest tmp_path so nothing touches the project tree.
  - Uses os.utime to age file mtimes past the grace cutoff when needed.

The four canonical scenarios:
  1. Orphan file (no ExportArtifact row) older than grace → deleted + counted.
  2. Orphan file newer than grace → kept (don't race an in-flight write).
  3. Stale temp partial (matching .tmp.<token>.<suffix> pattern) older than
     grace → deleted + counted.
  4. File backed by a non-expired ExportArtifact, aged → kept even if old.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db(transaction=True)

# Grace period shorter than the default so tests run quickly.
_GRACE = 3600  # 1 hour, matches the default


def _past_grace(export_dir: Path, name: str, seconds_ago: int = _GRACE + 60) -> Path:
    """Create a file in export_dir and age its mtime past the grace cutoff."""
    p = export_dir / name
    p.write_text("stale content")
    old = (datetime.now(tz=UTC) - timedelta(seconds=seconds_ago)).timestamp()
    os.utime(p, (old, old))
    return p


def _within_grace(export_dir: Path, name: str) -> Path:
    """Create a file in export_dir with a recent mtime (within grace period)."""
    p = export_dir / name
    p.write_text("fresh content")
    # mtime defaults to now — within grace by a large margin
    return p


def _make_export_artifact(export_dir: Path, token: str, fmt: str, expires_delta: timedelta):
    """Create a real ExportArtifact row and its payload file."""
    from apps.batches.models import BatchJob, ExportArtifact

    job = BatchJob.objects.create(secret_hash="hash", status="PENDING")
    file_path = export_dir / f"{token}.{fmt}"
    file_path.write_text("export content")
    expires_at = timezone.now() + expires_delta
    ExportArtifact.objects.create(
        job=job,
        fmt=fmt,
        token=token,
        path=str(file_path),
        checksum="a" * 64,  # 64-char hex to satisfy check constraint
        byte_size=14,
        rows_signature="b" * 64,  # 64-char hex to satisfy check constraint
        row_count=1,
        job_state_signature="c" * 64,
        expires_at=expires_at,
    )
    return file_path


# ---------------------------------------------------------------------------
# Test 1: orphan file without artifact is deleted after grace
# ---------------------------------------------------------------------------


def test_orphan_file_without_artifact_is_deleted_after_grace(tmp_path, monkeypatch, settings):
    """An orphan file (no ExportArtifact) older than the grace period is deleted."""
    from apps.operations.cleanup import cleanup_expired

    export_dir = tmp_path / "export_cache"
    export_dir.mkdir()
    monkeypatch.setattr("apps.batches.exports.EXPORT_DIR", export_dir)
    settings.EXPORT_RETENTION_DAYS = 14
    settings.EXPORT_ORPHAN_GRACE_SECONDS = _GRACE

    orphan = _past_grace(export_dir, "deadbeef1234567890123456789012.csv")

    result = cleanup_expired(actor="test")

    assert not orphan.exists(), "Orphan file should have been deleted"
    assert result.export_orphans_deleted >= 1


# ---------------------------------------------------------------------------
# Test 2: recent orphan within grace is kept
# ---------------------------------------------------------------------------


def test_recent_orphan_within_grace_is_kept(tmp_path, monkeypatch, settings):
    """A recently created orphan file (within grace) must NOT be deleted."""
    from apps.operations.cleanup import cleanup_expired

    export_dir = tmp_path / "export_cache"
    export_dir.mkdir()
    monkeypatch.setattr("apps.batches.exports.EXPORT_DIR", export_dir)
    settings.EXPORT_RETENTION_DAYS = 14
    settings.EXPORT_ORPHAN_GRACE_SECONDS = _GRACE

    fresh_orphan = _within_grace(export_dir, "freshbeef1234567890123456789012.csv")

    result = cleanup_expired(actor="test")

    assert fresh_orphan.exists(), "Recent orphan within grace should be kept"
    assert result.export_orphans_deleted == 0


# ---------------------------------------------------------------------------
# Test 3: stale temp file deleted
# ---------------------------------------------------------------------------


def test_stale_temp_file_deleted(tmp_path, monkeypatch, settings):
    """A stale .tmp.<token>.<suffix> partial from a crashed write is deleted."""
    from apps.operations.cleanup import cleanup_expired

    export_dir = tmp_path / "export_cache"
    export_dir.mkdir()
    monkeypatch.setattr("apps.batches.exports.EXPORT_DIR", export_dir)
    settings.EXPORT_RETENTION_DAYS = 14
    settings.EXPORT_ORPHAN_GRACE_SECONDS = _GRACE

    # Replicate the exact naming pattern from LocalArtifactStorage.open_atomic_write:
    # tempfile.mkstemp(dir=root, prefix=f".tmp.{token}.")
    fake_token = "a" * 43  # typical urlsafe token length
    stale_tmp = export_dir / f".tmp.{fake_token}.12345678"
    stale_tmp.write_text("partial write")
    old_ts = (datetime.now(tz=UTC) - timedelta(seconds=_GRACE + 120)).timestamp()
    os.utime(stale_tmp, (old_ts, old_ts))

    result = cleanup_expired(actor="test")

    assert not stale_tmp.exists(), "Stale temp file should have been deleted"
    assert result.export_orphans_deleted >= 1


# ---------------------------------------------------------------------------
# Test 4: live export file never deleted
# ---------------------------------------------------------------------------


def test_live_export_file_never_deleted(tmp_path, monkeypatch, settings):
    """A file backed by a non-expired ExportArtifact must NEVER be deleted,
    even if its mtime is older than the grace period."""
    from apps.operations.cleanup import cleanup_expired

    export_dir = tmp_path / "export_cache"
    export_dir.mkdir()
    monkeypatch.setattr("apps.batches.exports.EXPORT_DIR", export_dir)
    settings.EXPORT_RETENTION_DAYS = 14
    settings.EXPORT_ORPHAN_GRACE_SECONDS = _GRACE

    token = str(uuid.uuid4()).replace("-", "")[:43]
    live_file = _make_export_artifact(
        export_dir,
        token=token,
        fmt="csv",
        expires_delta=timedelta(days=7),  # not expired
    )

    # Age the file's mtime past the grace cutoff — should still be kept
    old_ts = (datetime.now(tz=UTC) - timedelta(seconds=_GRACE + 300)).timestamp()
    os.utime(live_file, (old_ts, old_ts))

    result = cleanup_expired(actor="test")

    assert live_file.exists(), "Live export file must never be deleted"
    assert result.export_orphans_deleted == 0


def test_live_legacy_sidecar_export_is_not_deleted_after_grace(tmp_path, monkeypatch, settings):
    """An unexpired legacy sidecar-only export remains valid without a DB row."""
    from apps.operations.cleanup import cleanup_expired

    export_dir = tmp_path / "export_cache"
    export_dir.mkdir()
    monkeypatch.setattr("apps.batches.exports.EXPORT_DIR", export_dir)
    settings.EXPORT_RETENTION_DAYS = 14
    settings.EXPORT_ORPHAN_GRACE_SECONDS = _GRACE

    token = "legacylive"
    payload = export_dir / f"{token}.csv"
    sidecar = export_dir / f"{token}.meta.json"
    payload.write_text("legacy export")
    sidecar.write_text(
        '{'
        f'"token": "{token}", '
        '"fmt": "csv", '
        f'"expires_at": "{(timezone.now() + timedelta(days=7)).isoformat()}"'
        '}'
    )
    old_ts = (datetime.now(tz=UTC) - timedelta(seconds=_GRACE + 300)).timestamp()
    os.utime(payload, (old_ts, old_ts))
    os.utime(sidecar, (old_ts, old_ts))

    result = cleanup_expired(actor="test")

    assert payload.exists(), "Live legacy payload should be kept until sidecar expiry"
    assert sidecar.exists(), "Live legacy sidecar should be kept until sidecar expiry"
    assert result.export_orphans_deleted == 0
