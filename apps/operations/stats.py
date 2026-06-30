"""Stats aggregation service for the operations dashboard."""
from __future__ import annotations

import shutil
from dataclasses import dataclass

from django.conf import settings
from django.db import connection


@dataclass
class DiskGuard:
    path: str
    total: int
    used: int
    free: int
    percent_used: float
    level: str  # "ok" | "warn" | "critical" | "emergency"
    upload_paused: bool


@dataclass
class WishartStats:
    success_count: int
    timeout_count: int
    server_error_count: int
    circuit_state: str
    last_success_at: str | None


@dataclass
class BackupStatus:
    latest_backup_at: str | None
    latest_backup_file: str | None
    latest_backup_checksum: str | None
    status: str


@dataclass
class SystemStats:
    unique_inchikey_count: int
    live_result_count: int
    cache_hit_rate: float
    pending_unique_structure_count: int
    wishart: WishartStats
    db_size_bytes: int
    backup: BackupStatus
    disk: DiskGuard


def _get_db_size_bytes() -> int:
    db_name = settings.DATABASES["default"]["NAME"]
    with connection.cursor() as cur:
        cur.execute("SELECT pg_database_size(%s)", [db_name])
        row = cur.fetchone()
    return int(row[0]) if row else 0


def _disk_guard(data_path: str | None = None) -> DiskGuard:
    path: str = data_path or str(getattr(settings, "BACKUP_DATA_PATH", "/"))
    usage = shutil.disk_usage(path)
    percent = (usage.used / usage.total * 100) if usage.total else 0.0
    pause_threshold = getattr(settings, "DISK_UPLOAD_PAUSE_PERCENT", 85)

    if percent >= 95:
        level = "emergency"
    elif percent >= 85:
        level = "critical"
    elif percent >= 70:
        level = "warn"
    else:
        level = "ok"

    return DiskGuard(
        path=path,
        total=usage.total,
        used=usage.used,
        free=usage.free,
        percent_used=round(percent, 2),
        level=level,
        upload_paused=(percent >= pause_threshold),
    )


def _wishart_stats() -> WishartStats:
    from apps.operations.models import SystemEvent

    success = SystemEvent.objects.filter(
        category="REMOTE", event_type="wishart_success"
    ).count()
    timeout = SystemEvent.objects.filter(
        category="REMOTE", event_type="wishart_timeout"
    ).count()
    server_err = SystemEvent.objects.filter(
        category="REMOTE", event_type="wishart_5xx"
    ).count()

    circuit_event = (
        SystemEvent.objects.filter(
            category="REMOTE", event_type__in=["circuit_open", "circuit_closed", "circuit_half_open"]
        )
        .order_by("-created_at")
        .first()
    )
    circuit_state = "closed"
    if circuit_event:
        mapping = {
            "circuit_open": "open",
            "circuit_closed": "closed",
            "circuit_half_open": "half_open",
        }
        circuit_state = mapping.get(circuit_event.event_type, "closed")

    last_success = (
        SystemEvent.objects.filter(category="REMOTE", event_type="wishart_success")
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )

    return WishartStats(
        success_count=success,
        timeout_count=timeout,
        server_error_count=server_err,
        circuit_state=circuit_state,
        last_success_at=last_success.isoformat() if last_success else None,
    )


def _backup_status() -> BackupStatus:
    from apps.operations.models import SystemEvent

    ev = (
        SystemEvent.objects.filter(category="BACKUP", event_type="backup_complete")
        .order_by("-created_at")
        .first()
    )
    if ev is None:
        return BackupStatus(
            latest_backup_at=None,
            latest_backup_file=None,
            latest_backup_checksum=None,
            status="never",
        )
    payload = ev.payload or {}
    return BackupStatus(
        latest_backup_at=ev.created_at.isoformat(),
        latest_backup_file=payload.get("file"),
        latest_backup_checksum=payload.get("checksum"),
        status=payload.get("status", "ok"),
    )


def collect_stats(data_path: str | None = None) -> SystemStats:
    from apps.classifications.models import ClassyfireLive
    from apps.remote.models import RemoteTask

    unique_inchikeys = ClassyfireLive.objects.values("inchikey").distinct().count()
    live_count = ClassyfireLive.objects.count()

    total_unique = unique_inchikeys
    cache_hit_rate = (live_count / total_unique) if total_unique > 0 else 0.0

    pending_unique = (
        RemoteTask.objects.exclude(state="DONE")
        .values("inchikey")
        .distinct()
        .count()
    )

    return SystemStats(
        unique_inchikey_count=unique_inchikeys,
        live_result_count=live_count,
        cache_hit_rate=round(cache_hit_rate, 4),
        pending_unique_structure_count=pending_unique,
        wishart=_wishart_stats(),
        db_size_bytes=_get_db_size_bytes(),
        backup=_backup_status(),
        disk=_disk_guard(data_path),
    )
