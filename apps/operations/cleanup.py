"""Cleanup expired uploads, exports, and old job secrets.

NEVER deletes classification cache (classyfire_live, classyfire_seed,
classification_history). Only removes expired operational/transient data.

Retention days are read from settings:
  UPLOAD_RETENTION_DAYS  (default 30) — BatchJobs older than N days get secret_hash wiped
  JOB_RETENTION_DAYS     (default 90) — BatchJobs completed more than N days ago get deleted
  EXPORT_RETENTION_DAYS  (default 14) — (reserved; no export model yet, logged only)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.core.enums import EventCategory
from apps.operations.models import SystemEvent


@dataclass
class CleanupResult:
    jobs_secrets_wiped: int
    jobs_deleted: int
    exports_deleted: int


def _retention_days(attr: str, default: int) -> int:
    return int(getattr(settings, attr, default))


def cleanup_expired(actor: str = "system") -> CleanupResult:
    """Run all retention cleanup tasks and log a SystemEvent."""
    from apps.batches.models import BatchJob
    from apps.core.enums import BatchJobStatus

    now = timezone.now()

    upload_days = _retention_days("UPLOAD_RETENTION_DAYS", 30)
    job_days = _retention_days("JOB_RETENTION_DAYS", 90)

    upload_cutoff = now - timedelta(days=upload_days)
    job_cutoff = now - timedelta(days=job_days)

    # Wipe job secrets for jobs older than UPLOAD_RETENTION_DAYS
    secrets_qs = BatchJob.objects.filter(
        created_at__lt=upload_cutoff
    ).exclude(secret_hash="")
    secrets_wiped = secrets_qs.update(secret_hash="")

    # Delete completed/failed jobs older than JOB_RETENTION_DAYS
    delete_qs = BatchJob.objects.filter(
        status__in=[BatchJobStatus.COMPLETE, BatchJobStatus.FAILED],
        created_at__lt=job_cutoff,
    )
    jobs_deleted, _ = delete_qs.delete()

    exports_deleted = 0

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
        },
    )

    return result
