"""Backup manifest, checksum, retention, and dump invocation.

The actual pg_dump/pg_restore subprocess is isolated behind _run_dump() so
tests can monkeypatch it without touching real binaries.

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


def _backup_tables() -> list[str]:
    tables = [
        "classyfire_live",
        "classification_history",
        "batch_jobs",
        "batch_items",
        "remote_tasks",
        "remote_queries",
        "remote_query_items",
        "chemont_nodes",
        "seed_imports",
        "system_events",
    ]
    if getattr(settings, "BACKUP_INCLUDE_SEED_TABLE", False):
        tables.append("classyfire_seed")
    return tables


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
# Core backup
# ---------------------------------------------------------------------------

def run_backup(backup_dir: Path | None = None) -> dict[str, Any]:
    """Run a backup: dump, checksum, manifest, retention, log event.

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
    tables = _backup_tables()

    table_args: list[str] = []
    for t in tables:
        table_args += ["-t", t]

    cmd = [
        pg_dump,
        "-h", db["host"],
        "-p", db["port"],
        "-U", db["user"],
        "-d", db["name"],
        "-Fc",
        "-f", str(dump_path),
        *table_args,
    ]

    result = _run_dump(cmd)

    checksum = _sha256_file(dump_path)

    manifest: dict[str, Any] = {
        "timestamp": now.isoformat(),
        "file": dump_filename,
        "checksum": checksum,
        "tables": tables,
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
    """Same as run_backup but accepts an injectable dump function (for tests)."""
    from apps.operations.models import SystemEvent

    backup_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(tz=UTC)
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    dump_filename = f"classyfire_{timestamp}.dump"
    dump_path = backup_dir / dump_filename

    db = _db_params()
    pg_dump = _pg_dump_bin()
    tables = _backup_tables()

    table_args: list[str] = []
    for t in tables:
        table_args += ["-t", t]

    cmd = [
        pg_dump,
        "-h", db["host"],
        "-p", db["port"],
        "-U", db["user"],
        "-d", db["name"],
        "-Fc",
        "-f", str(dump_path),
        *table_args,
    ]

    run_dump_fn(cmd, dump_path)

    checksum = _sha256_file(dump_path)

    manifest: dict[str, Any] = {
        "timestamp": now.isoformat(),
        "file": dump_filename,
        "checksum": checksum,
        "tables": tables,
        "status": "ok",
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
# Restore smoke test
# ---------------------------------------------------------------------------

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
