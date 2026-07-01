"""Audited admin operations for batch jobs.

These helpers keep Django ``ModelAdmin`` classes thin and avoid raw form edits
for stateful operations. They are intentionally service-level functions so the
admin UI and tests share the same safety checks.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Max

from apps.batches.models import BatchItem, BatchJob, ExportArtifact
from apps.core.enums import ACTIVE_TASK_STATES, BatchItemStatus, EventCategory
from apps.operations.models import SystemEvent
from apps.remote.models import RemoteTask

logger = logging.getLogger(__name__)

WAITING_REMOTE_STATUSES = (
    BatchItemStatus.PENDING_CLASSIFICATION,
    BatchItemStatus.REMOTE_DELAYED,
)


@dataclass(frozen=True)
class JobAdminOperationResult:
    """Summary returned by audited job admin operations."""

    jobs_deleted: int = 0
    jobs_updated: int = 0
    upload_artifacts_deleted: int = 0
    export_files_deleted: int = 0
    export_artifacts_deleted: int = 0
    remote_tasks_deleted: int = 0
    remote_tasks_updated: int = 0
    remote_tasks_skipped_shared: int = 0
    remote_tasks_skipped_leased: int = 0


@dataclass(frozen=True)
class _ExportFileRef:
    path: str
    token: str


@dataclass(frozen=True)
class _CollectedArtifacts:
    upload_paths: list[str]
    export_refs: list[_ExportFileRef]


def _job_id_strings(job_ids: Iterable[object]) -> list[str]:
    return [str(job_id) for job_id in job_ids]


def _collect_artifacts(job_ids: Sequence[object]) -> _CollectedArtifacts:
    upload_paths = [
        str(path)
        for path in BatchJob.objects.filter(pk__in=job_ids)
        .exclude(upload_artifact_path__isnull=True)
        .exclude(upload_artifact_path="")
        .values_list("upload_artifact_path", flat=True)
        if path
    ]
    export_refs = [
        _ExportFileRef(path=str(path), token=str(token))
        for path, token in ExportArtifact.objects.filter(job_id__in=job_ids).values_list(
            "path", "token"
        )
    ]
    return _CollectedArtifacts(upload_paths=upload_paths, export_refs=export_refs)


def _delete_file_if_exists(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        path.unlink()
    except FileNotFoundError:
        return 0
    except OSError:
        # Post-commit cleanup: a permission/IO error must not abort the whole
        # operation (the jobs are already deleted). Leave the file for the
        # periodic export/upload cleanup sweep to retry.
        logger.warning("failed to unlink admin artifact %s", path, exc_info=True)
        return 0
    return 1


def _unlink_artifacts(artifacts: _CollectedArtifacts) -> tuple[int, int]:
    from apps.batches.ingest import unlink_upload_artifact

    upload_deleted = sum(
        1 for relative_path in artifacts.upload_paths if unlink_upload_artifact(relative_path)
    )
    export_deleted = 0
    export_dir = Path(getattr(settings, "EXPORT_DIR", ""))
    for ref in artifacts.export_refs:
        export_deleted += _delete_file_if_exists(Path(ref.path))
        if export_dir:
            export_deleted += _delete_file_if_exists(
                export_dir / f"{ref.token}.meta.json"
            )
    return upload_deleted, export_deleted


def _audit(event_type: str, payload: dict[str, Any]) -> None:
    # Destructive / priority-changing admin operations are security-relevant, so
    # they land in the SECURITY audit stream rather than the WORKER one.
    SystemEvent.objects.create(
        category=EventCategory.SECURITY,
        event_type=event_type,
        payload=payload,
    )


def delete_jobs_with_artifacts(
    job_ids: Iterable[object],
    *,
    actor: str,
    event_type: str = "admin_delete_jobs",
    extra_payload: dict[str, Any] | None = None,
) -> JobAdminOperationResult:
    """Delete jobs and unlink their upload/export filesystem artifacts."""
    selected_ids = list(job_ids)
    with transaction.atomic():
        existing_ids = list(
            BatchJob.objects.filter(pk__in=selected_ids).values_list("pk", flat=True)
        )
        artifacts = _collect_artifacts(existing_ids)
        export_artifact_count = len(artifacts.export_refs)
        BatchJob.objects.filter(pk__in=existing_ids).delete()

    upload_deleted, export_files_deleted = _unlink_artifacts(artifacts)
    result = JobAdminOperationResult(
        jobs_deleted=len(existing_ids),
        upload_artifacts_deleted=upload_deleted,
        export_files_deleted=export_files_deleted,
        export_artifacts_deleted=export_artifact_count,
    )
    payload: dict[str, Any] = {
        "actor": actor,
        "job_ids": _job_id_strings(existing_ids),
        "jobs_deleted": result.jobs_deleted,
        "upload_artifacts_deleted": result.upload_artifacts_deleted,
        "export_files_deleted": result.export_files_deleted,
        "export_artifacts_deleted": result.export_artifacts_deleted,
    }
    if extra_payload:
        payload.update(extra_payload)
    _audit(event_type, payload)
    return result


def _waiting_inchikeys_for_jobs(job_ids: Sequence[object]) -> set[str]:
    return {
        inchikey
        for inchikey in BatchItem.objects.filter(
            job_id__in=job_ids,
            status__in=WAITING_REMOTE_STATUSES,
            inchikey__isnull=False,
        )
        .exclude(inchikey="")
        .values_list("inchikey", flat=True)
        .distinct()
        if inchikey
    }


def _shared_waiting_inchikeys(
    inchikeys: set[str],
    *,
    excluding_job_ids: Sequence[object],
) -> set[str]:
    if not inchikeys:
        return set()
    return {
        inchikey
        for inchikey in BatchItem.objects.filter(
            inchikey__in=inchikeys,
            status__in=WAITING_REMOTE_STATUSES,
        )
        .exclude(job_id__in=excluding_job_ids)
        .values_list("inchikey", flat=True)
        .distinct()
        if inchikey
    }


def recompute_remote_priorities_for_inchikeys(
    inchikeys: Iterable[str],
) -> tuple[int, int]:
    """Set active unleased remote task priority to max waiting job priority.

    Returns ``(updated, skipped_leased)``.
    """
    keys = {key for key in inchikeys if key}
    if not keys:
        return 0, 0

    priorities = {
        str(row["inchikey"]): int(row["priority"] or 0)
        for row in BatchItem.objects.filter(
            inchikey__in=keys,
            status__in=WAITING_REMOTE_STATUSES,
        )
        .values("inchikey")
        .annotate(priority=Max("job__priority"))
    }
    updated = 0
    for inchikey in keys:
        priority = priorities.get(inchikey, 0)
        updated += (
            RemoteTask.objects.filter(
                inchikey=inchikey,
                state__in=ACTIVE_TASK_STATES,
                lock_owner__isnull=True,
            )
            .exclude(priority=priority)
            .update(priority=priority)
        )
    skipped_leased = RemoteTask.objects.filter(
        inchikey__in=keys,
        state__in=ACTIVE_TASK_STATES,
        lock_owner__isnull=False,
    ).count()
    return updated, skipped_leased


def cancel_and_delete_jobs(
    job_ids: Iterable[object],
    *,
    actor: str,
) -> JobAdminOperationResult:
    """Cancel unshared remote work, then delete the selected jobs."""
    selected_ids = list(job_ids)
    with transaction.atomic():
        existing_ids = list(
            BatchJob.objects.filter(pk__in=selected_ids).values_list("pk", flat=True)
        )
        artifacts = _collect_artifacts(existing_ids)
        export_artifact_count = len(artifacts.export_refs)
        wanted_inchikeys = _waiting_inchikeys_for_jobs(existing_ids)
        # Lock the deletable (active, unleased) tasks for the wanted keys FIRST,
        # then re-check sharing as late as possible — immediately before the
        # delete — so a job that started waiting on the same InChIKey after we
        # began keeps its task (its item must not be orphaned). Concurrent
        # enqueue reuses an existing task via a plain SELECT, so this narrows
        # but does not fully close the window; a manual admin op is rare enough
        # that the residual is acceptable.
        candidate_rows = list(
            RemoteTask.objects.select_for_update()
            .filter(
                inchikey__in=wanted_inchikeys,
                state__in=ACTIVE_TASK_STATES,
                lock_owner__isnull=True,
            )
            .values_list("pk", "inchikey")
        )
        shared_inchikeys = _shared_waiting_inchikeys(
            wanted_inchikeys,
            excluding_job_ids=existing_ids,
        )
        unshared_inchikeys = wanted_inchikeys - shared_inchikeys

        shared_task_count = RemoteTask.objects.filter(
            inchikey__in=shared_inchikeys,
            state__in=ACTIVE_TASK_STATES,
        ).count()
        delete_task_ids = [
            pk for pk, inchikey in candidate_rows if inchikey not in shared_inchikeys
        ]
        remote_tasks_deleted = len(delete_task_ids)
        RemoteTask.objects.filter(pk__in=delete_task_ids).delete()

        leased_task_count = RemoteTask.objects.filter(
            inchikey__in=unshared_inchikeys,
            state__in=ACTIVE_TASK_STATES,
            lock_owner__isnull=False,
        ).count()

        BatchJob.objects.filter(pk__in=existing_ids).delete()
        remote_tasks_updated, priority_skipped_leased = (
            recompute_remote_priorities_for_inchikeys(shared_inchikeys)
        )

    upload_deleted, export_files_deleted = _unlink_artifacts(artifacts)
    result = JobAdminOperationResult(
        jobs_deleted=len(existing_ids),
        upload_artifacts_deleted=upload_deleted,
        export_files_deleted=export_files_deleted,
        export_artifacts_deleted=export_artifact_count,
        remote_tasks_deleted=remote_tasks_deleted,
        remote_tasks_updated=remote_tasks_updated,
        remote_tasks_skipped_shared=shared_task_count,
        remote_tasks_skipped_leased=leased_task_count + priority_skipped_leased,
    )
    _audit(
        "admin_cancel_delete_jobs",
        {
            "actor": actor,
            "job_ids": _job_id_strings(existing_ids),
            "jobs_deleted": result.jobs_deleted,
            "upload_artifacts_deleted": result.upload_artifacts_deleted,
            "export_files_deleted": result.export_files_deleted,
            "export_artifacts_deleted": result.export_artifacts_deleted,
            "remote_tasks_deleted": result.remote_tasks_deleted,
            "remote_tasks_updated": result.remote_tasks_updated,
            "remote_tasks_skipped_shared": result.remote_tasks_skipped_shared,
            "remote_tasks_skipped_leased": result.remote_tasks_skipped_leased,
        },
    )
    return result


def set_job_priority(
    job_ids: Iterable[object],
    *,
    priority: int,
    actor: str,
) -> JobAdminOperationResult:
    """Set selected job priority and recompute related remote task priorities."""
    selected_ids = list(job_ids)
    with transaction.atomic():
        existing_ids = list(
            BatchJob.objects.filter(pk__in=selected_ids).values_list("pk", flat=True)
        )
        affected_inchikeys = _waiting_inchikeys_for_jobs(existing_ids)
        jobs_updated = BatchJob.objects.filter(pk__in=existing_ids).update(
            priority=priority
        )
        remote_updated, remote_skipped_leased = recompute_remote_priorities_for_inchikeys(
            affected_inchikeys
        )

    result = JobAdminOperationResult(
        jobs_updated=jobs_updated,
        remote_tasks_updated=remote_updated,
        remote_tasks_skipped_leased=remote_skipped_leased,
    )
    _audit(
        "admin_set_job_priority",
        {
            "actor": actor,
            "job_ids": _job_id_strings(existing_ids),
            "priority": priority,
            "jobs_updated": result.jobs_updated,
            "remote_tasks_updated": result.remote_tasks_updated,
            "remote_tasks_skipped_leased": result.remote_tasks_skipped_leased,
        },
    )
    return result
