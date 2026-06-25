"""Backup manifest, checksum, retention, and dump invocation.

The actual pg_dump/pg_restore subprocess is isolated behind _run_dump() so
tests can monkeypatch it without touching real binaries.

Dump strategy
─────────────
We dump the FULL database and EXCLUDE only the rebuildable seed tables.
The ``classyfire_seed*`` pattern covers:
  - classyfire_seed          — base seed table (re-importable from Zenodo)
  - classyfire_seed_v<N>     — versioned physical tables (same source)
  - classyfire_seed_current  — view (pg_dump skips views with --exclude-table)

Everything else — compounds, batch jobs/items with their submitted structures,
remote tasks + provenance, classification live/history, operations — IS dumped.

Retention policy (configurable via settings):
  - BACKUP_DAILY_COUNT   (default 7)
  - BACKUP_WEEKLY_COUNT  (default 4)
  - BACKUP_MONTHLY_COUNT (default 6)

The pg_dump binary path is read from settings.PG_DUMP_BIN (default "pg_dump")
so it can point to /home/user/micromamba/envs/cf-pg/bin/pg_dump in production.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.conf import settings

# ---------------------------------------------------------------------------
# Injectable subprocess boundary
# ---------------------------------------------------------------------------


def _run_dump(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run pg_dump/pg_restore. Isolated for monkeypatching in tests."""
    return subprocess.run(cmd, capture_output=True, text=True, check=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pg_dump_bin() -> str:
    return getattr(settings, "PG_DUMP_BIN", os.environ.get("PG_DUMP_BIN", "pg_dump"))


def _pg_restore_bin() -> str:
    return getattr(settings, "PG_RESTORE_BIN", os.environ.get("PG_RESTORE_BIN", "pg_restore"))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _db_params() -> dict[str, str]:
    db = settings.DATABASES["default"]
    return {
        "host": db.get("HOST", "localhost"),
        "port": str(db.get("PORT", "5432")),
        "name": db["NAME"],
        "user": db.get("USER", ""),
        "password": db.get("PASSWORD", ""),
    }


def _seed_exclude_pattern() -> str:
    """Return the pg_dump --exclude-table glob pattern for seed tables."""
    return "classyfire_seed*"


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def _classify_backup_age(ts: datetime, now: datetime) -> str:
    """Return 'daily', 'weekly', or 'monthly' bucket for a backup timestamp."""
    delta = now - ts
    if delta.days < 7:
        return "daily"
    elif delta.days < 28:
        return "weekly"
    else:
        return "monthly"


def apply_retention(backup_dir: Path, now: datetime | None = None) -> list[Path]:
    """Delete old backups per retention policy. Returns list of deleted paths."""
    if now is None:
        now = datetime.now(tz=UTC)

    daily_count = getattr(settings, "BACKUP_DAILY_COUNT", 7)
    weekly_count = getattr(settings, "BACKUP_WEEKLY_COUNT", 4)
    monthly_count = getattr(settings, "BACKUP_MONTHLY_COUNT", 6)

    manifests = sorted(backup_dir.glob("*.manifest.json"), reverse=True)

    daily: list[Path] = []
    weekly: list[Path] = []
    monthly: list[Path] = []

    for manifest_path in manifests:
        try:
            data = json.loads(manifest_path.read_text())
            ts = datetime.fromisoformat(data["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
        except (KeyError, ValueError, OSError):
            continue

        bucket = _classify_backup_age(ts, now)
        if bucket == "daily":
            daily.append(manifest_path)
        elif bucket == "weekly":
            weekly.append(manifest_path)
        else:
            monthly.append(manifest_path)

    deleted: list[Path] = []

    def _prune(lst: list[Path], keep: int) -> None:
        for p in lst[keep:]:
            try:
                data = json.loads(p.read_text())
                dump_path = backup_dir / data.get("file", "")
                if dump_path.exists():
                    dump_path.unlink()
                # Collect archive files to delete, starting with the legacy
                # media_file field (old manifests only have this).
                legacy_media = data.get("media_file") or ""
                archives_to_delete: set[str] = set()
                if legacy_media:
                    archives_to_delete.add(legacy_media)
                # Also prune every archive in artifact_archives (new manifests).
                # Each entry is {"file": "...tar.gz", "checksum": "..."}.
                artifact_archives = data.get("artifact_archives") or {}
                if isinstance(artifact_archives, dict):
                    for entry in artifact_archives.values():
                        try:
                            fname = entry["file"]
                            if fname:
                                archives_to_delete.add(fname)
                        except (KeyError, TypeError):
                            pass
                for fname in archives_to_delete:
                    archive_path = backup_dir / fname
                    if archive_path.exists():
                        archive_path.unlink()
            except (KeyError, OSError, json.JSONDecodeError):
                pass
            try:
                p.unlink()
            except OSError:
                pass
            deleted.append(p)

    _prune(daily, daily_count)
    _prune(weekly, weekly_count)
    _prune(monthly, monthly_count)

    return deleted


# ---------------------------------------------------------------------------
# Fail-closed path validation
# ---------------------------------------------------------------------------


class BackupConfigurationError(ValueError):
    """Backup configuration points at an unsafe artifact path."""


def validate_artifact_dir(value: str, *, backup_dir: Path, label: str) -> Path:
    raw = (value or "").strip()
    if not raw:
        raise BackupConfigurationError(f"{label} artifact path is empty")
    path = Path(raw).resolve()
    backup_root = backup_dir.resolve()
    forbidden = {Path("/").resolve(), Path.cwd().resolve(), backup_root}
    if path in forbidden:
        raise BackupConfigurationError(f"{label} artifact path is unsafe: {path}")
    # Reject if backup_root is inside path (artifact dir contains backup output).
    if backup_root == path or backup_root.is_relative_to(path):
        raise BackupConfigurationError(
            f"{label} artifact path contains backup output: {path}"
        )
    # Reject if path is nested inside backup_root (artifact dir is inside backup output dir).
    if path.is_relative_to(backup_root):
        raise BackupConfigurationError(
            f"{label} artifact path is nested inside backup output dir: {path}"
        )
    return path


# ---------------------------------------------------------------------------
# Archive helpers
# ---------------------------------------------------------------------------


def _archive_dir(src_dir: Path, archive_path: Path) -> str | None:
    """Create a tar.gz of src_dir at archive_path. Returns sha256 or None.

    Returns None if the directory is absent or empty (nothing to archive).
    """
    if not src_dir.exists() or not any(src_dir.iterdir()):
        return None
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(str(src_dir), arcname=src_dir.name)
    return _sha256_file(archive_path)


def _build_artifact_archives(
    backup_dir: Path,
    timestamp: str,
    artifact_dirs: dict[str, Path],
) -> dict[str, Any]:
    """Archive each non-empty directory in artifact_dirs and return the
    ``artifact_archives`` manifest sub-dict.

    Keys in artifact_dirs map to the logical name used in the manifest (e.g.
    "media", "exports", "source_artifacts").  Archives are written into
    backup_dir using the naming convention
    ``classyfire_{name}_{timestamp}.tar.gz``.
    """
    result: dict[str, Any] = {}
    for name, src_dir in artifact_dirs.items():
        archive_filename = f"classyfire_{name}_{timestamp}.tar.gz"
        archive_path = backup_dir / archive_filename
        checksum = _archive_dir(src_dir, archive_path)
        if checksum is not None:
            result[name] = {"file": archive_filename, "checksum": checksum}
    return result


# ---------------------------------------------------------------------------
# Core backup
# ---------------------------------------------------------------------------


def run_backup(backup_dir: Path | None = None) -> dict[str, Any]:
    """Run a backup: dump (excluding seed tables), archive media, checksum,
    manifest, retention, log event.

    Returns the manifest dict.
    """
    from apps.operations.models import SystemEvent

    if backup_dir is None:
        backup_dir = Path(getattr(settings, "BACKUP_DIR", "/backups"))
    backup_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(tz=UTC)
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    dump_filename = f"classyfire_{timestamp}.dump"
    dump_path = backup_dir / dump_filename

    db = _db_params()
    pg_dump = _pg_dump_bin()

    cmd = [
        pg_dump,
        "-h", db["host"],
        "-p", db["port"],
        "-U", db["user"],
        "-d", db["name"],
        "-Fc",
        f"--exclude-table={_seed_exclude_pattern()}",
        "-f", str(dump_path),
    ]

    result = _run_dump(cmd)

    checksum = _sha256_file(dump_path)

    # Build artifact_dirs with fail-closed path validation.
    artifact_dirs = {
        "media": validate_artifact_dir(
            getattr(settings, "MEDIA_ROOT", "/app/media"),
            backup_dir=backup_dir,
            label="media",
        ),
        "exports": validate_artifact_dir(
            getattr(settings, "EXPORT_DIR", "/app/export_files"),
            backup_dir=backup_dir,
            label="exports",
        ),
        "source_artifacts": validate_artifact_dir(
            getattr(settings, "SOURCE_ARTIFACT_DIR", "/app/source_artifacts"),
            backup_dir=backup_dir,
            label="source_artifacts",
        ),
    }
    artifact_archives = _build_artifact_archives(backup_dir, timestamp, artifact_dirs)

    # media_file / media_checksum: kept for backward compatibility with
    # existing callers that read these top-level keys.
    media_entry = artifact_archives.get("media")
    media_file = media_entry["file"] if media_entry else None
    media_checksum = media_entry["checksum"] if media_entry else None

    manifest: dict[str, Any] = {
        "timestamp": now.isoformat(),
        "file": dump_filename,
        "checksum": checksum,
        "media_file": media_file,
        "media_checksum": media_checksum,
        "artifact_archives": artifact_archives,
        "excludes": _seed_exclude_pattern(),
        "status": "ok",
        "stderr": result.stderr or "",
    }

    manifest_path = backup_dir / f"classyfire_{timestamp}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    SystemEvent.objects.create(
        category="BACKUP",
        event_type="backup_complete",
        payload=manifest,
    )

    apply_retention(backup_dir, now=now)

    return manifest


def run_backup_injectable(
    backup_dir: Path,
    run_dump_fn: Any,
) -> dict[str, Any]:
    """Same as run_backup but accepts an injectable dump function (for tests).

    run_dump_fn signature: (cmd: list[str], dump_path: Path) -> None
    The function is responsible for writing the dump file at dump_path.
    """
    from apps.operations.models import SystemEvent

    backup_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(tz=UTC)
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    dump_filename = f"classyfire_{timestamp}.dump"
    dump_path = backup_dir / dump_filename

    db = _db_params()
    pg_dump = _pg_dump_bin()

    cmd = [
        pg_dump,
        "-h", db["host"],
        "-p", db["port"],
        "-U", db["user"],
        "-d", db["name"],
        "-Fc",
        f"--exclude-table={_seed_exclude_pattern()}",
        "-f", str(dump_path),
    ]

    run_dump_fn(cmd, dump_path)

    checksum = _sha256_file(dump_path)

    # Archive all artifact directories with fail-closed path validation.
    artifact_dirs = {
        "media": validate_artifact_dir(
            getattr(settings, "MEDIA_ROOT", "/app/media"),
            backup_dir=backup_dir,
            label="media",
        ),
        "exports": validate_artifact_dir(
            getattr(settings, "EXPORT_DIR", "/app/export_files"),
            backup_dir=backup_dir,
            label="exports",
        ),
        "source_artifacts": validate_artifact_dir(
            getattr(settings, "SOURCE_ARTIFACT_DIR", "/app/source_artifacts"),
            backup_dir=backup_dir,
            label="source_artifacts",
        ),
    }
    artifact_archives = _build_artifact_archives(backup_dir, timestamp, artifact_dirs)

    # media_file / media_checksum: kept for backward compatibility with
    # existing callers that read these top-level keys.
    media_entry = artifact_archives.get("media")
    media_file = media_entry["file"] if media_entry else None
    media_checksum = media_entry["checksum"] if media_entry else None

    manifest: dict[str, Any] = {
        "timestamp": now.isoformat(),
        "file": dump_filename,
        "checksum": checksum,
        "media_file": media_file,
        "media_checksum": media_checksum,
        "artifact_archives": artifact_archives,
        "excludes": _seed_exclude_pattern(),
        "status": "ok",
        "stderr": "",
    }

    manifest_path = backup_dir / f"classyfire_{timestamp}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    SystemEvent.objects.create(
        category="BACKUP",
        event_type="backup_complete",
        payload=manifest,
    )

    apply_retention(backup_dir, now=now)

    return manifest


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def restore_from_manifest(
    manifest: dict[str, Any],
    backup_dir: Path,
    target_db: str,
    run_restore_fn: Any = None,
) -> None:
    """Restore a DB dump from a manifest dict into target_db.

    Validates the checksum before restoring.
    run_restore_fn: injectable for tests; signature (cmd: list, manifest: dict) -> None
    """
    dump_path = backup_dir / manifest["file"]

    # Validate checksum before restoring.
    actual = _sha256_file(dump_path)
    expected = manifest.get("checksum", "")
    if actual != expected:
        raise ValueError(
            f"Checksum mismatch for {manifest['file']}: "
            f"expected {expected}, got {actual}"
        )

    db = _db_params()
    pg_restore = _pg_restore_bin()

    cmd = [
        pg_restore,
        "-h", db["host"],
        "-p", db["port"],
        "-U", db["user"],
        "-d", target_db,
        "--clean",
        "--if-exists",
        str(dump_path),
    ]

    if run_restore_fn is not None:
        run_restore_fn(cmd, manifest)
    else:
        _run_dump(cmd)


def restore_smoke_test(
    backup_dir: Path | None = None,
    target_db: str | None = None,
    run_restore_fn: Any = None,
) -> dict[str, Any]:
    """Restore latest backup into target_db and run a smoke check.

    Logs result to system_events (category=BACKUP).
    run_restore_fn: injectable for tests; signature (cmd, manifest) -> None
    """
    from apps.operations.models import SystemEvent

    if backup_dir is None:
        backup_dir = Path(getattr(settings, "BACKUP_DIR", "/backups"))
    if target_db is None:
        target_db = getattr(settings, "RESTORE_SMOKE_DB", "classyfire_smoke_test")

    manifests = sorted(backup_dir.glob("*.manifest.json"), reverse=True)
    if not manifests:
        err_result: dict[str, Any] = {"status": "error", "error": "no_backup_found"}
        SystemEvent.objects.create(
            category="BACKUP",
            event_type="restore_smoke_test",
            payload=err_result,
        )
        return err_result

    latest = manifests[0]
    manifest = json.loads(latest.read_text())
    dump_path = backup_dir / manifest["file"]

    db = _db_params()
    pg_restore = _pg_restore_bin()

    cmd = [
        pg_restore,
        "-h", db["host"],
        "-p", db["port"],
        "-U", db["user"],
        "-d", target_db,
        "--clean",
        "--if-exists",
        str(dump_path),
    ]

    try:
        if run_restore_fn is not None:
            run_restore_fn(cmd, manifest)
        else:
            _run_dump(cmd)
        status = "ok"
        error = None
    except Exception as exc:
        status = "error"
        error = str(exc)

    result: dict[str, Any] = {
        "status": status,
        "backup_file": manifest.get("file"),
        "backup_checksum": manifest.get("checksum"),
        "target_db": target_db,
    }
    if error:
        result["error"] = error

    SystemEvent.objects.create(
        category="BACKUP",
        event_type="restore_smoke_test",
        payload=result,
    )
    return result
