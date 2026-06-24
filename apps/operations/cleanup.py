"""Cleanup expired uploads, exports, and old job secrets.

NEVER deletes classification cache (classyfire_live, classyfire_seed,
classification_history). Only removes expired operational/transient data.

Retention days are read from settings:
  UPLOAD_RETENTION_DAYS  (default 30) — BatchJobs older than N days get secret_hash wiped
  JOB_RETENTION_DAYS     (default 90) — BatchJobs completed more than N days ago get deleted
  EXPORT_RETENTION_DAYS  (default 14) — expired export cache files are deleted
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.core.enums import EventCategory
from apps.operations.models import SystemEvent

logger = logging.getLogger(__name__)


@dataclass
class CleanupResult:
    jobs_secrets_wiped: int
    jobs_deleted: int
    exports_deleted: int


_EXPORT_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_EXPORT_FORMATS = {"csv", "tsv", "json", "csv.gz", "tsv.gz"}
_EXPORT_PAYLOAD_RE = re.compile(r"^[A-Za-z0-9_-]+\.(?:csv|tsv|json|csv\.gz|tsv\.gz)$")


def _retention_days(attr: str, default: int) -> int:
    return int(getattr(settings, attr, default))


def _delete_if_exists(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        path.unlink()
    except FileNotFoundError:
        return 0
    return 1


def _safe_export_payload_path(export_dir: Path, token: str, fmt: str) -> Path | None:
    if not _EXPORT_TOKEN_RE.fullmatch(token) or fmt not in _EXPORT_FORMATS:
        return None
    export_root = export_dir.resolve()
    payload_path = (export_root / f"{token}.{fmt}").resolve()
    try:
        payload_path.relative_to(export_root)
    except ValueError:
        return None
    return payload_path


def _cleanup_expired_exports(now: datetime, retention_days: int) -> int:
    from apps.batches import exports
    from apps.batches.models import ExportArtifact

    export_dir = exports.EXPORT_DIR
    deleted = 0
    retained_payloads: set[Path] = set()

    # ── Phase 1: prune expired ExportArtifact rows (DB source of truth) ──────
    expired_artifacts = ExportArtifact.objects.filter(expires_at__lte=now)
    for artifact in expired_artifacts.iterator(chunk_size=500):
        artifact_path = Path(artifact.path)
        file_deleted = _delete_if_exists(artifact_path)
        # Also delete the legacy sidecar if present
        if export_dir.exists():
            sidecar = export_dir / f"{artifact.token}.meta.json"
            _delete_if_exists(sidecar)
        artifact.delete()
        deleted += 1 + file_deleted
        logger.info(
            "Pruned expired export artifact token=%s fmt=%s expires_at=%s",
            artifact.token[:12],
            artifact.fmt,
            artifact.expires_at.isoformat(),
        )

    # ── Phase 2: legacy sidecar-based cleanup (backward compat) ──────────────
    if not export_dir.exists():
        return deleted

    cutoff = now - timedelta(days=retention_days)

    for meta_path in export_dir.glob("*.meta.json"):
        should_delete = False
        payload_path: Path | None = None
        try:
            raw = json.loads(meta_path.read_text())
            token = str(raw["token"])
            fmt = str(raw["fmt"])
            expires_at = datetime.fromisoformat(str(raw["expires_at"]))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            payload_path = _safe_export_payload_path(export_dir, token, fmt)
            should_delete = expires_at <= now
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            should_delete = True

        if should_delete:
            deleted += _delete_if_exists(meta_path)
            if payload_path is not None:
                deleted += _delete_if_exists(payload_path)
        elif payload_path is not None:
            retained_payloads.add(payload_path)

    for payload_path in export_dir.iterdir():
        resolved_payload = payload_path.resolve()
        if (
            not payload_path.is_file()
            or payload_path.name.endswith(".meta.json")
            or not _EXPORT_PAYLOAD_RE.fullmatch(payload_path.name)
            or resolved_payload in retained_payloads
        ):
            continue
        modified_at = datetime.fromtimestamp(payload_path.stat().st_mtime, tz=UTC)
        if modified_at < cutoff:
            deleted += _delete_if_exists(payload_path)

    return deleted


def cleanup_expired(actor: str = "system") -> CleanupResult:
    """Run all retention cleanup tasks and log a SystemEvent."""
    from apps.batches.models import BatchJob
    from apps.core.enums import BatchJobStatus

    now = timezone.now()

    upload_days = _retention_days("UPLOAD_RETENTION_DAYS", 30)
    job_days = _retention_days("JOB_RETENTION_DAYS", 90)
    export_days = _retention_days("EXPORT_RETENTION_DAYS", 14)

    upload_cutoff = now - timedelta(days=upload_days)
    job_cutoff = now - timedelta(days=job_days)

    # Wipe job secrets for jobs older than UPLOAD_RETENTION_DAYS
    secrets_qs = BatchJob.objects.filter(
        created_at__lt=upload_cutoff
    ).exclude(secret_hash="")
    secrets_wiped = secrets_qs.update(secret_hash="")

    # Delete completed/failed jobs older than JOB_RETENTION_DAYS
    terminal_statuses = [
        BatchJobStatus.COMPLETE,
        BatchJobStatus.COMPLETE_WITH_ACTION_REQUIRED,
        BatchJobStatus.FAILED,
    ]
    delete_qs = BatchJob.objects.filter(status__in=terminal_statuses).filter(
        Q(completed_at__lt=job_cutoff)
        | Q(completed_at__isnull=True, created_at__lt=job_cutoff)
    )
    jobs_deleted, _ = delete_qs.delete()

    exports_deleted = _cleanup_expired_exports(now, export_days)

    result = CleanupResult(
        jobs_secrets_wiped=secrets_wiped,
        jobs_deleted=jobs_deleted,
        exports_deleted=exports_deleted,
    )

    SystemEvent.objects.create(
        category=EventCategory.WORKER,
        event_type="cleanup_expired",
        payload={
            "actor": actor,
            "jobs_secrets_wiped": secrets_wiped,
            "jobs_deleted": jobs_deleted,
            "exports_deleted": exports_deleted,
            "upload_retention_days": upload_days,
            "job_retention_days": job_days,
            "export_retention_days": export_days,
        },
    )

    return result
