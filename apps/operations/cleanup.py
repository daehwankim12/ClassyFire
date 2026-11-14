"""Cleanup expired uploads, exports, and old job secrets.

NEVER deletes classification cache (classyfire_live,
classification_history). Only removes expired operational/transient data.

Retention days are read from settings:
  UPLOAD_RETENTION_DAYS        (default 30)   — BatchJobs older than N days get secret_hash wiped
  JOB_RETENTION_DAYS           (default 7)    — BatchJobs completed more than N days ago get deleted
  EXPORT_RETENTION_DAYS        (default 7)    — expired export cache files are deleted
  EXPORT_ORPHAN_GRACE_SECONDS  (default 3600) — grace window before an orphan/temp file is removed
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
    # exports_deleted counts EXPORT ARTIFACTS pruned: one increment per pruned
    # ExportArtifact row AND one per legacy sidecar-tracked export removed.  It
    # does NOT count individual filesystem files (payload + sidecar) separately;
    # those are tracked by export_files_deleted.
    exports_deleted: int
    export_files_deleted: int = 0
    # export_orphans_deleted counts files in EXPORT_DIR with no live
    # ExportArtifact row (rollback orphans, stale .tmp.* partials) that were
    # older than EXPORT_ORPHAN_GRACE_SECONDS and have been unlinked.
    export_orphans_deleted: int = 0
    # upload_temps_deleted counts stale *.tmp partials in UPLOAD_ARTIFACT_DIR
    # (crashed spool writes) older than the upload-temp grace window.
    upload_temps_deleted: int = 0
    # upload_artifacts_deleted counts FINAL spooled upload artifacts unlinked
    # because their owning job was pruned by retention (R-07).
    upload_artifacts_deleted: int = 0
    # upload_artifacts_released counts raw uploads of staged/terminal jobs
    # released (file removed, byte reservation cleared) ahead of retention.
    upload_artifacts_released: int = 0
    # upload_orphans_deleted counts final-name upload files with no BatchJob
    # row, older than the upload-temp grace window.
    upload_orphans_deleted: int = 0
    # jobs_kept_for_unlink_failure counts expired jobs NOT deleted this run
    # because their raw upload could not be removed (retried next run).
    jobs_kept_for_unlink_failure: int = 0


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


def _cleanup_expired_exports(now: datetime, retention_days: int) -> tuple[int, int]:
    """Prune expired exports.

    Returns ``(artifacts_pruned, files_deleted)`` where ``artifacts_pruned`` is
    the number of distinct exports removed (one per ExportArtifact row plus one
    per legacy sidecar-tracked export) and ``files_deleted`` is the number of
    individual filesystem files unlinked (payloads + sidecars).
    """
    from apps.batches import exports
    from apps.batches.models import ExportArtifact

    export_dir = exports.EXPORT_DIR
    artifacts_pruned = 0
    files_deleted = 0
    pruned_tokens: set[str] = set()
    retained_payloads: set[Path] = set()

    # ── Phase 1: prune expired ExportArtifact rows (DB source of truth) ──────
    expired_artifacts = ExportArtifact.objects.filter(expires_at__lte=now)
    for artifact in expired_artifacts.iterator(chunk_size=500):
        files_deleted += _delete_if_exists(Path(artifact.path))
        # Also delete the legacy sidecar if present
        if export_dir.exists():
            sidecar = export_dir / f"{artifact.token}.meta.json"
            files_deleted += _delete_if_exists(sidecar)
        pruned_tokens.add(artifact.token)
        artifact.delete()
        artifacts_pruned += 1
        logger.info(
            "Pruned expired export artifact token=%s fmt=%s expires_at=%s",
            artifact.token[:12],
            artifact.fmt,
            artifact.expires_at.isoformat(),
        )

    # ── Phase 2: legacy sidecar-based cleanup (backward compat) ──────────────
    if not export_dir.exists():
        return artifacts_pruned, files_deleted

    cutoff = now - timedelta(days=retention_days)

    for meta_path in export_dir.glob("*.meta.json"):
        should_delete = False
        payload_path: Path | None = None
        token: str | None = None
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

        # Skip tokens already accounted for in Phase 1 (DB-tracked).
        already_pruned = token is not None and token in pruned_tokens

        if should_delete:
            removed = _delete_if_exists(meta_path)
            files_deleted += removed
            if payload_path is not None:
                files_deleted += _delete_if_exists(payload_path)
            if not already_pruned and removed:
                artifacts_pruned += 1
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
            files_deleted += _delete_if_exists(payload_path)

    return artifacts_pruned, files_deleted


def _cleanup_orphan_exports(now: datetime, grace_seconds: int) -> int:
    """Delete orphaned export files and stale temp partials in EXPORT_DIR.

    An "orphan" is any regular file in EXPORT_DIR whose basename is NOT in the
    set of live (non-expired) ExportArtifact file basenames AND whose mtime is
    older than ``now - grace_seconds``.  This covers both:
    - Payload files whose transaction rolled back (no DB row was ever written).
    - Stale ``.tmp.<token>.<suffix>`` partials from crashed streaming writes
      (pattern: ``tempfile.mkstemp(prefix=".tmp.<token>.")``, a dotfile name).

    Files referenced by a non-expired ExportArtifact are ALWAYS kept, even if
    their mtime is old.  Files newer than the grace cutoff are also kept so we
    never race an in-flight atomic write.

    Path safety: every candidate is resolved and checked to lie inside
    EXPORT_DIR before unlinking.  Symlinks and directory entries are skipped.

    Returns the count of files deleted.
    """
    from apps.batches import exports
    from apps.batches.models import ExportArtifact

    export_dir = exports.EXPORT_DIR
    if not export_dir.exists():
        return 0

    export_root = export_dir.resolve()
    grace_cutoff = now - timedelta(seconds=grace_seconds)

    # Build the set of basenames that have a live (non-expired) ExportArtifact.
    # This is the authoritative "do not delete" set.
    live_basenames: set[str] = {
        Path(a.path).name
        for a in ExportArtifact.objects.filter(expires_at__gt=now).only("path")
    }

    # Also include sidecar files for live tokens so we never accidentally remove
    # a .meta.json whose payload is still live.
    live_sidecar_basenames: set[str] = {
        f"{a.token}.meta.json"
        for a in ExportArtifact.objects.filter(expires_at__gt=now).only("token")
    }
    protected_basenames = live_basenames | live_sidecar_basenames

    # Legacy sidecar-only exports remain valid until their sidecar expires.
    # The expired-export phase retains their payloads, and this later orphan
    # pass must preserve both the payload and sidecar even without a DB row.
    for meta_path in export_dir.glob("*.meta.json"):
        try:
            raw = json.loads(meta_path.read_text())
            token = str(raw["token"])
            fmt = str(raw["fmt"])
            expires_at = datetime.fromisoformat(str(raw["expires_at"]))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            payload_path = _safe_export_payload_path(export_dir, token, fmt)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if expires_at > now and payload_path is not None:
            protected_basenames.add(meta_path.name)
            protected_basenames.add(payload_path.name)

    orphans_deleted = 0
    for entry in export_dir.iterdir():
        # Only regular files — skip directories and symlinks.
        if not entry.is_file() or entry.is_symlink():
            continue

        # Path-safety: resolve and verify the file is inside the export root.
        try:
            resolved = entry.resolve()
            resolved.relative_to(export_root)
        except (ValueError, OSError):
            continue

        # Never touch files that belong to a live ExportArtifact.
        if entry.name in protected_basenames:
            continue

        # Grace period: skip files that might still be part of an in-flight write.
        try:
            mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        if mtime >= grace_cutoff:
            continue

        deleted = _delete_if_exists(entry)
        if deleted:
            orphans_deleted += 1
            logger.info(
                "Deleted orphan export file name=%s mtime=%s",
                entry.name,
                mtime.isoformat(),
            )

    return orphans_deleted


def _cleanup_stale_upload_temps() -> int:
    """Remove stale ``*.tmp`` upload-spool partials (H-04), returning the count.

    A ``.tmp`` older than the grace window is a partial from a crashed spool
    write (the atomic ``replace()`` never ran), safe to drop. Guarded so a
    misconfigured upload root never aborts the whole cleanup run.
    """
    from apps.batches.ingest import cleanup_stale_upload_temps

    grace = int(getattr(settings, "UPLOAD_TEMP_GRACE_SECONDS", 86400))
    try:
        return cleanup_stale_upload_temps(max_age_seconds=grace)
    except ValueError:
        logger.warning("Skipping stale upload-temp cleanup: invalid UPLOAD_ARTIFACT_DIR")
        return 0


def _release_unneeded_upload_artifacts() -> int:
    from apps.batches.services import release_unneeded_upload_artifacts

    try:
        return release_unneeded_upload_artifacts()
    except ValueError:
        logger.warning("Skipping upload artifact release: invalid UPLOAD_ARTIFACT_DIR")
        return 0


def _cleanup_orphan_upload_artifacts() -> int:
    """Remove final-name upload files that no BatchJob references.

    Such files (a crash between spool and commit, a restore, a manual copy) are
    otherwise charged against the global retained-byte cap forever. Only files
    older than the upload-temp grace window are touched, so a spool whose job
    row has not committed yet is never removed.
    """
    import time

    from apps.batches.ingest import validate_upload_root
    from apps.batches.models import BatchJob

    try:
        root = validate_upload_root(getattr(settings, "UPLOAD_ARTIFACT_DIR", ""))
    except ValueError:
        logger.warning("Skipping upload orphan cleanup: invalid UPLOAD_ARTIFACT_DIR")
        return 0
    if not root.exists():
        return 0
    grace = int(getattr(settings, "UPLOAD_TEMP_GRACE_SECONDS", 86400))
    cutoff = time.time() - grace
    candidates: dict[str, Path] = {}
    for path in root.iterdir():
        try:
            if (
                path.is_file()
                and not path.is_symlink()
                and not path.name.endswith(".tmp")
                and path.stat().st_mtime <= cutoff
            ):
                candidates[path.name] = path
        except FileNotFoundError:
            continue
    if not candidates:
        return 0
    referenced = set(
        BatchJob.objects.filter(upload_artifact_path__in=list(candidates))
        .values_list("upload_artifact_path", flat=True)
    )
    removed = 0
    for name, path in candidates.items():
        if name in referenced:
            continue
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            logger.warning("could not remove orphan upload artifact %s", path, exc_info=True)
    return removed


def cleanup_expired(actor: str = "system") -> CleanupResult:
    """Run all retention cleanup tasks and log a SystemEvent."""
    from apps.batches.models import BatchJob
    from apps.core.enums import BatchJobStatus

    now = timezone.now()

    upload_days = _retention_days("UPLOAD_RETENTION_DAYS", 30)
    job_days = _retention_days("JOB_RETENTION_DAYS", 7)
    export_days = _retention_days("EXPORT_RETENTION_DAYS", 7)

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
    # Collect linked FINAL upload artifact paths BEFORE deleting the rows — once
    # the jobs are gone the relative paths are unrecoverable. Unlink each before
    # deleting its byte reservation so an I/O failure cannot undercount storage.
    from apps.batches.ingest import unlink_upload_artifact

    # Release raw uploads that staged/terminal jobs no longer need first, so
    # their bytes stop counting against the quotas well before job retention.
    upload_artifacts_released = _release_unneeded_upload_artifacts()

    # Materialise the expired job ids once: re-evaluating the lazy queryset at
    # delete time could pick up rows whose artifacts were never unlinked.
    expired_job_ids = list(delete_qs.values_list("pk", flat=True))
    upload_paths = list(
        BatchJob.objects.filter(pk__in=expired_job_ids)
        .exclude(upload_artifact_path__isnull=True)
        .exclude(upload_artifact_path="")
        .values_list("pk", "upload_artifact_path")
    )

    # ExportArtifact rows cascade when their owning BatchJob is deleted.  Collect
    # and unlink their payloads/sidecars before deleting the jobs so fresh
    # exports created near the job-retention cutoff cannot survive as
    # sidecar-only downloads after their result page has been pruned.
    from apps.batches import exports
    from apps.batches.models import ExportArtifact

    expiring_artifacts = list(
        ExportArtifact.objects.filter(job_id__in=expired_job_ids).only("path", "token")
    )
    cascaded_export_files_deleted = 0
    for artifact in expiring_artifacts:
        cascaded_export_files_deleted += _delete_if_exists(Path(artifact.path))
        if exports.EXPORT_DIR.exists():
            cascaded_export_files_deleted += _delete_if_exists(
                exports.EXPORT_DIR / f"{artifact.token}.meta.json"
            )

    upload_artifacts_deleted = 0
    unlink_failed_job_ids: set = set()
    for job_id, relative_path in upload_paths:
        try:
            if unlink_upload_artifact(relative_path):
                upload_artifacts_deleted += 1
        except OSError:
            # Keep this job (and its byte reservation) for the next run instead
            # of aborting retention, export and orphan cleanup for everyone.
            logger.warning(
                "could not remove upload artifact for expired job %s; keeping it",
                job_id,
                exc_info=True,
            )
            unlink_failed_job_ids.add(job_id)
    deletable_job_ids = [
        job_id for job_id in expired_job_ids if job_id not in unlink_failed_job_ids
    ]
    jobs_deleted, _ = BatchJob.objects.filter(pk__in=deletable_job_ids).delete()

    exports_deleted, export_files_deleted = _cleanup_expired_exports(now, export_days)
    exports_deleted += len(expiring_artifacts)
    export_files_deleted += cascaded_export_files_deleted

    orphan_grace = _retention_days("EXPORT_ORPHAN_GRACE_SECONDS", 3600)
    export_orphans_deleted = _cleanup_orphan_exports(now, orphan_grace)

    # Remove stale *.tmp partials from crashed upload-spool writes (H-04).
    upload_temps_deleted = _cleanup_stale_upload_temps()
    upload_orphans_deleted = _cleanup_orphan_upload_artifacts()

    result = CleanupResult(
        jobs_secrets_wiped=secrets_wiped,
        jobs_deleted=jobs_deleted,
        exports_deleted=exports_deleted,
        export_files_deleted=export_files_deleted,
        export_orphans_deleted=export_orphans_deleted,
        upload_temps_deleted=upload_temps_deleted,
        upload_artifacts_deleted=upload_artifacts_deleted,
        upload_artifacts_released=upload_artifacts_released,
        upload_orphans_deleted=upload_orphans_deleted,
        jobs_kept_for_unlink_failure=len(unlink_failed_job_ids),
    )

    SystemEvent.objects.create(
        category=EventCategory.WORKER,
        event_type="cleanup_expired",
        payload={
            "actor": actor,
            "jobs_secrets_wiped": secrets_wiped,
            "jobs_deleted": jobs_deleted,
            "exports_deleted": exports_deleted,
            "export_files_deleted": export_files_deleted,
            "export_orphans_deleted": export_orphans_deleted,
            "upload_temps_deleted": upload_temps_deleted,
            "upload_artifacts_deleted": upload_artifacts_deleted,
            "upload_artifacts_released": upload_artifacts_released,
            "upload_orphans_deleted": upload_orphans_deleted,
            "jobs_kept_for_unlink_failure": len(unlink_failed_job_ids),
            "upload_retention_days": upload_days,
            "job_retention_days": job_days,
            "export_retention_days": export_days,
            "export_orphan_grace_seconds": orphan_grace,
        },
    )

    return result
