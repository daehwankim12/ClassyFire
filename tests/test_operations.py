"""Tests for Task 10: Stats, Admin Operations, Backup, and Cleanup.

All tests run against the real Postgres DB. Subprocess calls (pg_dump /
pg_restore) are monkeypatched so no binary is required. Classification cache
tables (classyfire_live, classyfire_seed, classification_history) must never
be deleted by cleanup.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.utils import timezone as dj_timezone

from apps.operations.models import SeedImport, SystemEvent

pytestmark = pytest.mark.django_db(transaction=True)

VALID_IK_A = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"  # aspirin 27-char
VALID_IK_B = "RYYVLZVUVIJVGH-UHFFFAOYSA-N"  # caffeine 27-char
VALID_IK_C = "WQZGKKKJIJFFOK-GASJEMHNSA-N"  # glucose  27-char


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _live(inchikey: str):
    from apps.classifications.models import ClassyfireLive

    return ClassyfireLive.objects.create(
        inchikey=inchikey,
        source_type="WISHART_LIVE_QUERY",
        source_priority=100,
        retrieved_at=dj_timezone.now(),
    )


def _seed(inchikey: str, seed_source_id: int = 1):
    from apps.classifications.models import ClassyfireSeed

    return ClassyfireSeed.objects.create(
        inchikey=inchikey,
        inchikey14=inchikey[:14],
        seed_source_id=seed_source_id,
        seed_version="test-v1",
    )


def _remote_task(inchikey: str, state: str = "READY"):
    from apps.remote.models import RemoteTask

    return RemoteTask.objects.create(inchikey=inchikey, state=state)


def _batch_job(status: str = "PENDING", created_at_override=None):
    from apps.batches.models import BatchJob

    job = BatchJob.objects.create(secret_hash="abc123", status=status)
    if created_at_override:
        BatchJob.objects.filter(pk=job.pk).update(created_at=created_at_override)
        job.refresh_from_db()
    return job


def _seed_import(**kwargs):
    defaults = {
        "doi": "10.5281/zenodo.test",
        "filename": "seed_test.tsv.zst",
        "checksum": "abcdef123456",
        "import_status": "COMPLETE",
        "actual_row_count": 100,
        "who": "tester",
    }
    defaults.update(kwargs)
    return SeedImport.objects.create(**defaults)


def _system_event(category: str, event_type: str, payload: dict | None = None):
    return SystemEvent.objects.create(
        category=category,
        event_type=event_type,
        payload=payload or {},
    )


# ---------------------------------------------------------------------------
# Stats: unique InChIKey count
# ---------------------------------------------------------------------------


def test_stats_unique_inchikey_count():
    from apps.operations.stats import collect_stats

    _live(VALID_IK_A)
    _live(VALID_IK_B)
    stats = collect_stats("/")
    assert stats.unique_inchikey_count >= 2


def test_stats_unique_inchikey_no_duplicates():
    from apps.operations.stats import collect_stats

    _live(VALID_IK_A)
    count_before = collect_stats("/").unique_inchikey_count
    count_after = collect_stats("/").unique_inchikey_count
    assert count_before == count_after


# ---------------------------------------------------------------------------
# Stats: live result count
# ---------------------------------------------------------------------------


def test_stats_live_result_count():
    from apps.operations.stats import collect_stats

    before = collect_stats("/").live_result_count
    _live(VALID_IK_A)
    after = collect_stats("/").live_result_count
    assert after == before + 1


# ---------------------------------------------------------------------------
# Stats: seed count
# ---------------------------------------------------------------------------


def test_stats_seed_count():
    from apps.operations.stats import collect_stats

    before = collect_stats("/").seed_count
    _seed(VALID_IK_A)
    after = collect_stats("/").seed_count
    assert after == before + 1


# ---------------------------------------------------------------------------
# Stats: cache hit rate
# ---------------------------------------------------------------------------


def test_stats_cache_hit_rate_zero_when_no_data():
    from apps.classifications.models import ClassyfireLive
    from apps.operations.stats import collect_stats

    ClassyfireLive.objects.all().delete()
    stats = collect_stats("/")
    assert stats.cache_hit_rate == 0.0


def test_stats_cache_hit_rate_positive_when_data():
    from apps.operations.stats import collect_stats

    _live(VALID_IK_A)
    stats = collect_stats("/")
    assert stats.cache_hit_rate > 0


# ---------------------------------------------------------------------------
# Stats: pending unique structure count
# ---------------------------------------------------------------------------


def test_stats_pending_unique_structure_count():
    from apps.operations.stats import collect_stats

    before = collect_stats("/").pending_unique_structure_count
    _remote_task(VALID_IK_A, state="READY")
    after = collect_stats("/").pending_unique_structure_count
    assert after == before + 1


def test_stats_done_tasks_not_counted_as_pending():
    from apps.operations.stats import collect_stats

    _remote_task(VALID_IK_B, state="DONE")
    before = collect_stats("/").pending_unique_structure_count
    _remote_task(VALID_IK_C, state="DONE")
    after = collect_stats("/").pending_unique_structure_count
    assert before == after


# ---------------------------------------------------------------------------
# Stats: Wishart success / timeout / 5xx + circuit state
# ---------------------------------------------------------------------------


def test_stats_wishart_success_count():
    from apps.operations.stats import collect_stats

    before = collect_stats("/").wishart.success_count
    _system_event("REMOTE", "wishart_success")
    _system_event("REMOTE", "wishart_success")
    after = collect_stats("/").wishart.success_count
    assert after == before + 2


def test_stats_wishart_timeout_count():
    from apps.operations.stats import collect_stats

    before = collect_stats("/").wishart.timeout_count
    _system_event("REMOTE", "wishart_timeout")
    after = collect_stats("/").wishart.timeout_count
    assert after == before + 1


def test_stats_wishart_5xx_count():
    from apps.operations.stats import collect_stats

    before = collect_stats("/").wishart.server_error_count
    _system_event("REMOTE", "wishart_5xx")
    _system_event("REMOTE", "wishart_5xx")
    _system_event("REMOTE", "wishart_5xx")
    after = collect_stats("/").wishart.server_error_count
    assert after == before + 3


def test_stats_wishart_circuit_state_open():
    from apps.operations.stats import collect_stats

    _system_event("REMOTE", "circuit_closed")
    _system_event("REMOTE", "circuit_open")
    stats = collect_stats("/")
    assert stats.wishart.circuit_state == "open"


def test_stats_wishart_circuit_state_closed():
    from apps.operations.stats import collect_stats

    _system_event("REMOTE", "circuit_open")
    _system_event("REMOTE", "circuit_closed")
    stats = collect_stats("/")
    assert stats.wishart.circuit_state == "closed"


def test_stats_wishart_circuit_state_default_closed_when_no_event():
    from apps.operations.stats import collect_stats

    SystemEvent.objects.filter(
        category="REMOTE",
        event_type__in=["circuit_open", "circuit_closed", "circuit_half_open"],
    ).delete()
    stats = collect_stats("/")
    assert stats.wishart.circuit_state == "closed"


def test_stats_wishart_last_success_at_none_when_no_event():
    from apps.operations.stats import collect_stats

    SystemEvent.objects.filter(category="REMOTE", event_type="wishart_success").delete()
    stats = collect_stats("/")
    assert stats.wishart.last_success_at is None


def test_stats_wishart_last_success_at_populated():
    from apps.operations.stats import collect_stats

    _system_event("REMOTE", "wishart_success")
    stats = collect_stats("/")
    assert stats.wishart.last_success_at is not None


# ---------------------------------------------------------------------------
# Stats: seed import history
# ---------------------------------------------------------------------------


def test_stats_seed_import_history_empty():
    from apps.operations.stats import collect_stats

    SeedImport.objects.all().delete()
    stats = collect_stats("/")
    assert stats.seed_import_history == []


def test_stats_seed_import_history_populated():
    from apps.operations.stats import collect_stats

    SeedImport.objects.all().delete()
    _seed_import(import_status="COMPLETE")
    _seed_import(import_status="PENDING")
    stats = collect_stats("/")
    assert len(stats.seed_import_history) == 2
    statuses = {r["import_status"] for r in stats.seed_import_history}
    assert "COMPLETE" in statuses
    assert "PENDING" in statuses


# ---------------------------------------------------------------------------
# Stats: DB size
# ---------------------------------------------------------------------------


def test_stats_db_size_positive():
    from apps.operations.stats import collect_stats

    stats = collect_stats("/")
    assert stats.db_size_bytes > 0


# ---------------------------------------------------------------------------
# Stats: backup status
# ---------------------------------------------------------------------------


def test_stats_backup_status_never_when_no_event():
    from apps.operations.stats import collect_stats

    SystemEvent.objects.filter(category="BACKUP", event_type="backup_complete").delete()
    stats = collect_stats("/")
    assert stats.backup.status == "never"
    assert stats.backup.latest_backup_at is None


def test_stats_backup_status_populated():
    from apps.operations.stats import collect_stats

    SystemEvent.objects.filter(category="BACKUP", event_type="backup_complete").delete()
    _system_event("BACKUP", "backup_complete", {"file": "cf_20260101.dump", "checksum": "abc", "status": "ok"})
    stats = collect_stats("/")
    assert stats.backup.status == "ok"
    assert stats.backup.latest_backup_file == "cf_20260101.dump"
    assert stats.backup.latest_backup_checksum == "abc"
    assert stats.backup.latest_backup_at is not None


# ---------------------------------------------------------------------------
# Stats: disk guard levels
# ---------------------------------------------------------------------------


def test_disk_guard_level_ok_below_70():
    import unittest.mock as mock

    from apps.operations.stats import _disk_guard

    with mock.patch("shutil.disk_usage") as m:
        m.return_value = mock.Mock(total=100, used=60, free=40)
        result = _disk_guard("/")
    assert result.level == "ok"
    assert result.percent_used == 60.0
    assert result.upload_paused is False


def test_disk_guard_level_warn_at_70():
    import unittest.mock as mock

    from apps.operations.stats import _disk_guard

    with mock.patch("shutil.disk_usage") as m:
        m.return_value = mock.Mock(total=100, used=70, free=30)
        result = _disk_guard("/")
    assert result.level == "warn"
    assert result.upload_paused is False


def test_disk_guard_level_critical_at_85():
    import unittest.mock as mock

    from apps.operations.stats import _disk_guard

    with mock.patch("shutil.disk_usage") as m:
        m.return_value = mock.Mock(total=100, used=85, free=15)
        result = _disk_guard("/")
    assert result.level == "critical"
    assert result.upload_paused is True


def test_disk_guard_level_emergency_at_95():
    import unittest.mock as mock

    from apps.operations.stats import _disk_guard

    with mock.patch("shutil.disk_usage") as m:
        m.return_value = mock.Mock(total=100, used=95, free=5)
        result = _disk_guard("/")
    assert result.level == "emergency"
    assert result.upload_paused is True


def test_disk_guard_upload_paused_at_threshold():
    """DISK_UPLOAD_PAUSE_PERCENT=85 means paused at 85%."""
    import unittest.mock as mock

    from apps.operations.stats import _disk_guard

    with mock.patch("shutil.disk_usage") as m:
        m.return_value = mock.Mock(total=100, used=84, free=16)
        result = _disk_guard("/")
    assert result.upload_paused is False

    with mock.patch("shutil.disk_usage") as m:
        m.return_value = mock.Mock(total=100, used=85, free=15)
        result = _disk_guard("/")
    assert result.upload_paused is True


# ---------------------------------------------------------------------------
# Admin: remote pause / resume
# ---------------------------------------------------------------------------


def test_admin_pause_remote():
    from apps.operations.admin_views import is_remote_paused, pause_remote

    SystemEvent.objects.filter(
        category="WORKER", event_type__in=["remote_pause", "remote_resume"]
    ).delete()
    assert is_remote_paused() is False
    pause_remote(actor="test")
    assert is_remote_paused() is True


def test_admin_resume_remote():
    from apps.operations.admin_views import is_remote_paused, pause_remote, resume_remote

    pause_remote(actor="test")
    assert is_remote_paused() is True
    resume_remote(actor="test")
    assert is_remote_paused() is False


def test_admin_not_paused_when_no_events():
    from apps.operations.admin_views import is_remote_paused

    SystemEvent.objects.filter(
        category="WORKER", event_type__in=["remote_pause", "remote_resume"]
    ).delete()
    assert is_remote_paused() is False


# ---------------------------------------------------------------------------
# Admin: retry failed/deferred tasks
# ---------------------------------------------------------------------------


def test_admin_retry_deferred_tasks():
    from apps.operations.admin_views import retry_failed_tasks

    t_deferred = _remote_task(VALID_IK_A, state="DEFERRED")
    count = retry_failed_tasks(actor="test")
    assert count >= 1
    t_deferred.refresh_from_db()
    assert t_deferred.state == "READY"
    assert t_deferred.next_run_at is not None


def test_admin_retry_retry_wait_tasks():
    """RETRY_WAIT (backoff-parked) tasks must also be reset to READY."""
    from apps.operations.admin_views import retry_failed_tasks

    t_retry_wait = _remote_task(VALID_IK_B, state="RETRY_WAIT")
    count = retry_failed_tasks(actor="test")
    assert count >= 1
    t_retry_wait.refresh_from_db()
    assert t_retry_wait.state == "READY"
    assert t_retry_wait.next_run_at is not None


def test_admin_retry_does_not_reset_done_or_running():
    """DONE (terminal) and RUNNING (in-flight) tasks must NOT be force-reset."""
    from apps.operations.admin_views import retry_failed_tasks

    t_done = _remote_task(VALID_IK_A, state="DONE")
    t_running = _remote_task(VALID_IK_B, state="RUNNING")
    retry_failed_tasks(actor="test")
    t_done.refresh_from_db()
    t_running.refresh_from_db()
    assert t_done.state == "DONE"
    assert t_running.state == "RUNNING"


def test_admin_retry_logs_event():
    from apps.operations.admin_views import retry_failed_tasks

    before = SystemEvent.objects.filter(
        category="WORKER", event_type="retry_deferred_tasks"
    ).count()
    retry_failed_tasks(actor="test")
    after = SystemEvent.objects.filter(
        category="WORKER", event_type="retry_deferred_tasks"
    ).count()
    assert after == before + 1


# ---------------------------------------------------------------------------
# Admin: seed activation trigger
# ---------------------------------------------------------------------------


def test_admin_trigger_seed_activation():
    from apps.operations.admin_views import trigger_seed_activation

    ev = trigger_seed_activation(seed_import_id=42, actor="test")
    assert ev.category == "SEED"
    assert ev.event_type == "seed_activation_triggered"
    assert ev.payload["seed_import_id"] == 42


# ---------------------------------------------------------------------------
# Admin: backup status review
# ---------------------------------------------------------------------------


def test_admin_backup_status_never():
    from apps.operations.admin_views import get_backup_status

    SystemEvent.objects.filter(category="BACKUP", event_type="backup_complete").delete()
    result = get_backup_status()
    assert result["status"] == "never"


def test_admin_backup_status_ok():
    from apps.operations.admin_views import get_backup_status

    SystemEvent.objects.filter(category="BACKUP", event_type="backup_complete").delete()
    _system_event("BACKUP", "backup_complete", {"file": "cf_test.dump", "checksum": "xyz", "status": "ok"})
    result = get_backup_status()
    assert result["status"] == "ok"
    assert result["file"] == "cf_test.dump"
    assert result["checksum"] == "xyz"
    assert result["latest_backup_at"] is not None


# ---------------------------------------------------------------------------
# Backup: manifest, checksum, retention (subprocess mocked)
# ---------------------------------------------------------------------------


def _fake_dump_fn(cmd: list, dump_path: Path) -> None:
    """Write a dummy dump file (simulates pg_dump)."""
    dump_path.write_bytes(b"dummy-dump-content-for-testing")


def test_backup_creates_manifest_and_event(tmp_path):
    from apps.operations.backup import run_backup_injectable

    before = SystemEvent.objects.filter(
        category="BACKUP", event_type="backup_complete"
    ).count()
    manifest = run_backup_injectable(tmp_path, _fake_dump_fn)
    after = SystemEvent.objects.filter(
        category="BACKUP", event_type="backup_complete"
    ).count()

    assert after == before + 1
    assert manifest["status"] == "ok"
    assert manifest["checksum"]
    assert "file" in manifest
    manifest_files = list(tmp_path.glob("*.manifest.json"))
    assert len(manifest_files) == 1


def test_backup_checksum_matches_file(tmp_path):
    import hashlib

    from apps.operations.backup import run_backup_injectable

    manifest = run_backup_injectable(tmp_path, _fake_dump_fn)
    dump_path = tmp_path / manifest["file"]
    assert dump_path.exists()
    checksum = hashlib.sha256(dump_path.read_bytes()).hexdigest()
    assert manifest["checksum"] == checksum


def test_backup_tables_listed_in_manifest(tmp_path):
    from apps.operations.backup import run_backup_injectable

    manifest = run_backup_injectable(tmp_path, _fake_dump_fn)
    assert "classyfire_live" in manifest["tables"]
    assert "batch_jobs" in manifest["tables"]
    assert "system_events" in manifest["tables"]
    assert "classyfire_seed" not in manifest["tables"]


# ---------------------------------------------------------------------------
# Backup: retention pruning
# ---------------------------------------------------------------------------


def _write_manifest(backup_dir: Path, ts: datetime, filename: str = "test.dump") -> Path:
    dump_path = backup_dir / filename
    dump_path.write_bytes(b"x")
    manifest = {
        "timestamp": ts.isoformat(),
        "file": filename,
        "checksum": "abc",
        "status": "ok",
    }
    stem = filename.replace(".dump", "")
    mpath = backup_dir / f"{stem}.manifest.json"
    mpath.write_text(json.dumps(manifest))
    return mpath


def test_retention_prunes_old_daily(tmp_path):
    from apps.operations.backup import apply_retention

    now = datetime.now(tz=UTC)
    # 8 daily backups — should prune 1 (keep 7)
    for i in range(8):
        ts = now - timedelta(hours=i)
        _write_manifest(tmp_path, ts, f"cf_{i:02d}.dump")

    apply_retention(tmp_path, now=now)
    remaining = list(tmp_path.glob("*.manifest.json"))
    assert len(remaining) == 7


def test_retention_keeps_weekly_within_limit(tmp_path):
    from apps.operations.backup import apply_retention

    now = datetime.now(tz=UTC)
    # 5 weekly backups (7-27 days ago) — keep 4
    for i in range(5):
        ts = now - timedelta(days=7 + i)
        _write_manifest(tmp_path, ts, f"cf_weekly_{i}.dump")

    apply_retention(tmp_path, now=now)
    remaining = list(tmp_path.glob("*.manifest.json"))
    assert len(remaining) == 4


def test_retention_keeps_monthly_within_limit(tmp_path):
    from apps.operations.backup import apply_retention

    now = datetime.now(tz=UTC)
    # 7 monthly backups (28+ days ago) — keep 6
    for i in range(7):
        ts = now - timedelta(days=28 + i)
        _write_manifest(tmp_path, ts, f"cf_monthly_{i}.dump")

    apply_retention(tmp_path, now=now)
    remaining = list(tmp_path.glob("*.manifest.json"))
    assert len(remaining) == 6


# ---------------------------------------------------------------------------
# Restore smoke test: event logged (subprocess mocked)
# ---------------------------------------------------------------------------


def test_restore_smoke_test_logs_event(tmp_path):
    from apps.operations.backup import restore_smoke_test

    _write_manifest(tmp_path, datetime.now(tz=UTC), "cf_latest.dump")

    calls = []

    def fake_restore(cmd: list, manifest: dict) -> None:
        calls.append(cmd)

    before = SystemEvent.objects.filter(
        category="BACKUP", event_type="restore_smoke_test"
    ).count()
    result = restore_smoke_test(
        backup_dir=tmp_path,
        target_db="classyfire_smoke_test",
        run_restore_fn=fake_restore,
    )
    after = SystemEvent.objects.filter(
        category="BACKUP", event_type="restore_smoke_test"
    ).count()

    assert after == before + 1
    assert result["status"] == "ok"
    assert len(calls) == 1


def test_restore_smoke_test_no_backup_logs_error():
    from apps.operations.backup import restore_smoke_test

    with tempfile.TemporaryDirectory() as tmp:
        empty_dir = Path(tmp)
        before = SystemEvent.objects.filter(
            category="BACKUP", event_type="restore_smoke_test"
        ).count()
        result = restore_smoke_test(
            backup_dir=empty_dir,
            target_db="classyfire_smoke_test",
        )
        after = SystemEvent.objects.filter(
            category="BACKUP", event_type="restore_smoke_test"
        ).count()

    assert after == before + 1
    assert result["status"] == "error"
    assert "no_backup_found" in result.get("error", "")


# ---------------------------------------------------------------------------
# Cleanup: expired job secrets wiped, old jobs deleted
# ---------------------------------------------------------------------------


def test_cleanup_wipes_old_job_secrets():
    from apps.operations.cleanup import cleanup_expired

    old_time = dj_timezone.now() - timedelta(days=35)
    job = _batch_job(status="COMPLETE", created_at_override=old_time)
    assert job.secret_hash == "abc123"

    cleanup_expired(actor="test")
    job.refresh_from_db()
    assert job.secret_hash == ""


def test_cleanup_deletes_old_completed_jobs():
    from apps.batches.models import BatchJob
    from apps.operations.cleanup import cleanup_expired

    old_time = dj_timezone.now() - timedelta(days=95)
    job = _batch_job(status="COMPLETE", created_at_override=old_time)
    pk = job.pk

    cleanup_expired(actor="test")
    assert not BatchJob.objects.filter(pk=pk).exists()


def test_cleanup_keeps_recent_jobs():
    from apps.batches.models import BatchJob
    from apps.operations.cleanup import cleanup_expired

    job = _batch_job(status="COMPLETE")
    pk = job.pk

    cleanup_expired(actor="test")
    assert BatchJob.objects.filter(pk=pk).exists()


def test_cleanup_never_deletes_classification_cache():
    from apps.classifications.models import ClassificationHistory, ClassyfireLive, ClassyfireSeed
    from apps.operations.cleanup import cleanup_expired

    _live(VALID_IK_A)
    _seed(VALID_IK_B)

    live_count_before = ClassyfireLive.objects.count()
    seed_count_before = ClassyfireSeed.objects.count()
    history_count_before = ClassificationHistory.objects.count()

    cleanup_expired(actor="test")

    assert ClassyfireLive.objects.count() == live_count_before
    assert ClassyfireSeed.objects.count() == seed_count_before
    assert ClassificationHistory.objects.count() == history_count_before


def test_cleanup_logs_system_event():
    from apps.operations.cleanup import cleanup_expired

    before = SystemEvent.objects.filter(
        category="WORKER", event_type="cleanup_expired"
    ).count()
    cleanup_expired(actor="test")
    after = SystemEvent.objects.filter(
        category="WORKER", event_type="cleanup_expired"
    ).count()
    assert after == before + 1


def test_cleanup_does_not_delete_pending_jobs():
    from apps.batches.models import BatchJob
    from apps.operations.cleanup import cleanup_expired

    old_time = dj_timezone.now() - timedelta(days=200)
    job = _batch_job(status="PENDING", created_at_override=old_time)
    pk = job.pk

    cleanup_expired(actor="test")
    # PENDING jobs should NOT be deleted (only COMPLETE/FAILED)
    assert BatchJob.objects.filter(pk=pk).exists()


def test_cleanup_removes_expired_exports_and_uploads(tmp_path, monkeypatch, settings):
    from apps.batches.models import BatchJob
    from apps.operations.cleanup import cleanup_expired

    export_dir = tmp_path / "export_cache"
    export_dir.mkdir()
    monkeypatch.setattr("apps.batches.exports.EXPORT_DIR", export_dir)
    settings.EXPORT_RETENTION_DAYS = 14
    settings.UPLOAD_RETENTION_DAYS = 30

    old_export = export_dir / "old-token.csv"
    old_meta = export_dir / "old-token.meta.json"
    fresh_export = export_dir / "fresh-token.csv"
    fresh_meta = export_dir / "fresh-token.meta.json"

    old_export.write_text("expired")
    old_meta.write_text(
        json.dumps(
            {
                "token": "old-token",
                "job_id": "job-1",
                "fmt": "csv",
                "expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            }
        )
    )
    fresh_export.write_text("fresh")
    fresh_meta.write_text(
        json.dumps(
            {
                "token": "fresh-token",
                "job_id": "job-2",
                "fmt": "csv",
                "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            }
        )
    )

    old_time = dj_timezone.now() - timedelta(days=35)
    upload_job = _batch_job(status="PENDING", created_at_override=old_time)

    result = cleanup_expired(actor="test")

    upload_job.refresh_from_db()
    assert upload_job.secret_hash == ""
    assert result.jobs_secrets_wiped == 1
    assert result.exports_deleted == 2
    assert not old_export.exists()
    assert not old_meta.exists()
    assert fresh_export.exists()
    assert fresh_meta.exists()
    assert BatchJob.objects.filter(pk=upload_job.pk).exists()


def test_cleanup_export_metadata_cannot_delete_path_traversal_payload(
    tmp_path, monkeypatch, settings
):
    from apps.operations.cleanup import cleanup_expired

    export_dir = tmp_path / "export_cache"
    export_dir.mkdir()
    outside_payload = tmp_path / "escape.csv"
    outside_payload.write_text("do not delete")
    monkeypatch.setattr("apps.batches.exports.EXPORT_DIR", export_dir)
    settings.EXPORT_RETENTION_DAYS = 14

    (export_dir / "evil.meta.json").write_text(
        json.dumps(
            {
                "token": "../escape",
                "job_id": "job-1",
                "fmt": "csv",
                "expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            }
        )
    )

    cleanup_expired(actor="test")

    assert outside_payload.exists()


def test_cleanup_export_metadata_unsupported_fmt_does_not_delete_payload(
    tmp_path, monkeypatch, settings
):
    from apps.operations.cleanup import cleanup_expired

    export_dir = tmp_path / "export_cache"
    export_dir.mkdir()
    monkeypatch.setattr("apps.batches.exports.EXPORT_DIR", export_dir)
    settings.EXPORT_RETENTION_DAYS = 14

    unsupported_payload = export_dir / "old-token.html"
    unsupported_payload.write_text("not an export payload")
    (export_dir / "old-token.meta.json").write_text(
        json.dumps(
            {
                "token": "old-token",
                "job_id": "job-1",
                "fmt": "html",
                "expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            }
        )
    )

    cleanup_expired(actor="test")

    assert unsupported_payload.exists()


def test_cleanup_orphan_exports_only_deletes_known_payload_files(
    tmp_path, monkeypatch, settings
):
    from apps.operations.cleanup import cleanup_expired

    export_dir = tmp_path / "export_cache"
    export_dir.mkdir()
    monkeypatch.setattr("apps.batches.exports.EXPORT_DIR", export_dir)
    settings.EXPORT_RETENTION_DAYS = 14

    old = (datetime.now(UTC) - timedelta(days=20)).timestamp()
    orphan_payload = export_dir / "orphan-token.csv"
    unrelated_file = export_dir / "notes.txt"
    unrelated_dir = export_dir / "old-dir.csv"
    orphan_payload.write_text("delete")
    unrelated_file.write_text("keep")
    unrelated_dir.mkdir()
    os.utime(orphan_payload, (old, old))
    os.utime(unrelated_file, (old, old))
    os.utime(unrelated_dir, (old, old))

    cleanup_expired(actor="test")

    assert not orphan_payload.exists()
    assert unrelated_file.exists()
    assert unrelated_dir.exists()


def test_cleanup_deletes_old_action_required_jobs_by_completed_at():
    from apps.batches.models import BatchJob
    from apps.core.enums import BatchJobStatus
    from apps.operations.cleanup import cleanup_expired

    job = _batch_job(status=BatchJobStatus.COMPLETE_WITH_ACTION_REQUIRED)
    old_completed_at = dj_timezone.now() - timedelta(days=95)
    BatchJob.objects.filter(pk=job.pk).update(completed_at=old_completed_at)

    cleanup_expired(actor="test")

    assert not BatchJob.objects.filter(pk=job.pk).exists()
