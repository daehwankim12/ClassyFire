"""Audited admin operations for batch jobs.

These helpers keep Django ``ModelAdmin`` classes thin and avoid raw form edits
for stateful operations. They are intentionally service-level functions so the
admin UI and tests share the same safety checks.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Max

from apps.batches.models import BatchItem, BatchJob, ExportArtifact
from apps.batches.remote_delay import BUDGET_EXCEEDED_MESSAGE
from apps.core.enums import ACTIVE_TASK_STATES, BatchItemStatus, EventCategory, TaskState
from apps.operations.models import SystemEvent
from apps.remote.models import RemoteTask

logger = logging.getLogger(__name__)

WAITING_REMOTE_STATUSES = (
    BatchItemStatus.PENDING_CLASSIFICATION,
    BatchItemStatus.REMOTE_DELAYED,
)

CANCEL_DELETE_TASK_STATES = (
    TaskState.READY,
    TaskState.RETRY_WAIT,
    TaskState.DEFERRED,
)
REQUEUE_TIMED_OUT_CHUNK_SIZE = 1_000
# Keep admin POSTs bounded. Operators commonly requeue one changelist page
# immediately; larger selections are handed to the durable ingest-worker retry.
REQUEUE_TIMED_OUT_INLINE_LIMIT = 100
REMOTE_QUEUE_BACKPRESSURE_MESSAGE = "Remote queue is at capacity; retrying later."


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
    items_requeued: int = 0
    items_deferred: int = 0
    items_skipped_not_timed_out: int = 0
    items_skipped_not_budget_delayed: int = 0
    items_skipped_budget: int = 0
    items_skipped_backpressure: int = 0


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


def _chunks(values: Sequence[object], size: int) -> Iterable[Sequence[object]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _remote_queue_at_capacity() -> bool:
    max_depth = int(getattr(settings, "REMOTE_MAX_QUEUE_DEPTH", 0))
    return (
        RemoteTask.objects.filter(state__in=ACTIVE_TASK_STATES).count() >= max_depth
    )


def _bulk_mark_backpressured(
    items: Sequence[BatchItem],
) -> tuple[int, set[uuid.UUID], set[int]]:
    item_ids = [item.pk for item in items]
    if not item_ids:
        return 0, set(), set()
    updated = BatchItem.objects.filter(
        pk__in=item_ids,
        status=BatchItemStatus.REMOTE_TIMED_OUT,
    ).update(
        status=BatchItemStatus.REMOTE_DELAYED,
        remote_task=None,
        message=REMOTE_QUEUE_BACKPRESSURE_MESSAGE,
    )
    return updated, {item.job_id for item in items}, set(item_ids)


def _bulk_defer_timed_out_items(
    item_ids: Sequence[object],
) -> tuple[int, int, set[uuid.UUID]]:
    deferred = 0
    skipped_not_timed_out = 0
    affected_job_ids: set[uuid.UUID] = set()

    for chunk_ids in _chunks(item_ids, REQUEUE_TIMED_OUT_CHUNK_SIZE):
        with transaction.atomic():
            existing_count = BatchItem.objects.filter(pk__in=chunk_ids).count()
            retryable_rows = list(
                BatchItem.objects.filter(
                    pk__in=chunk_ids,
                    status=BatchItemStatus.REMOTE_TIMED_OUT,
                    inchikey__isnull=False,
                )
                .exclude(inchikey="")
                .values_list("pk", "job_id")
            )
            retryable_ids = [pk for pk, _job_id in retryable_rows]
            updated = 0
            if retryable_ids:
                updated = BatchItem.objects.filter(
                    pk__in=retryable_ids,
                    status=BatchItemStatus.REMOTE_TIMED_OUT,
                ).update(
                    status=BatchItemStatus.REMOTE_DELAYED,
                    remote_task=None,
                    message=REMOTE_QUEUE_BACKPRESSURE_MESSAGE,
                )
            deferred += updated
            skipped_not_timed_out += existing_count - updated
            affected_job_ids.update(job_id for _pk, job_id in retryable_rows)

    return deferred, skipped_not_timed_out, affected_job_ids


def _release_jobs_for_remote_retry(job_ids: Iterable[object]) -> None:
    """Make jobs immediately claimable again after an operator requeue.

    Clearing ``retry_not_before`` matters as much as clearing the claim: a job
    parked on a budget/capacity denial carries a retry gate, and an operator who
    just requeued its rows means "now", not "when the gate elapses".
    """
    ids = list(job_ids)
    if not ids:
        return
    BatchJob.objects.filter(pk__in=ids).update(
        claimed_by=None,
        claimed_at=None,
        processing_error=None,
        retry_not_before=None,
    )


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


def _unlink_upload_artifacts(artifacts: _CollectedArtifacts) -> int:
    from apps.batches.ingest import unlink_upload_artifact

    return sum(
        1 for relative_path in artifacts.upload_paths if unlink_upload_artifact(relative_path)
    )


def _unlink_export_artifacts(artifacts: _CollectedArtifacts) -> int:
    export_deleted = 0
    export_dir = Path(getattr(settings, "EXPORT_DIR", ""))
    for ref in artifacts.export_refs:
        export_deleted += _delete_file_if_exists(Path(ref.path))
        if export_dir:
            export_deleted += _delete_file_if_exists(
                export_dir / f"{ref.token}.meta.json"
            )
    return export_deleted


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
        # Upload files are removed before their owning rows release retained-byte
        # reservations. An unlink error aborts the transaction and keeps them
        # accounted for; missing files are already safe to release.
        upload_deleted = _unlink_upload_artifacts(artifacts)
        BatchJob.objects.filter(pk__in=existing_ids).delete()

    export_files_deleted = _unlink_export_artifacts(artifacts)
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
    keys = sorted({key for key in inchikeys if key})
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
        upload_deleted = _unlink_upload_artifacts(artifacts)
        wanted_inchikeys = _waiting_inchikeys_for_jobs(existing_ids)
        # Lock the deletable unleased tasks for the wanted keys FIRST, then
        # re-check sharing as late as possible — immediately before the delete
        # — so a job that started waiting on the same InChIKey after we began
        # keeps its task (its item must not be orphaned). Only pre-submission
        # queue states are eligible: submitted/polling work can carry live
        # RemoteSubmissionMember reservations that must be released by the
        # normal query terminal paths, not by deleting the task underneath them.
        # Concurrent enqueue reuses an existing task via a plain SELECT, so this
        # narrows but does not fully close the window; a manual admin op is rare
        # enough that the residual is acceptable.
        candidate_rows = list(
            RemoteTask.objects.select_for_update()
            .filter(
                inchikey__in=wanted_inchikeys,
                state__in=CANCEL_DELETE_TASK_STATES,
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
        ).exclude(
            state__in=CANCEL_DELETE_TASK_STATES,
            lock_owner__isnull=True,
        ).count()

        BatchJob.objects.filter(pk__in=existing_ids).delete()
        remote_tasks_updated, priority_skipped_leased = (
            recompute_remote_priorities_for_inchikeys(shared_inchikeys)
        )

    export_files_deleted = _unlink_export_artifacts(artifacts)
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

    # Reprioritizing a large job can touch tens of thousands of RemoteTask rows.
    # Keep that out of the BatchJob update transaction so admin actions do not
    # hold job locks while competing with workers that already hold task locks.
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


def requeue_timed_out_items(
    item_ids: Iterable[object],
    *,
    actor: str,
) -> JobAdminOperationResult:
    """Re-queue REMOTE_TIMED_OUT items for another remote attempt.

    For each selected item still in ``REMOTE_TIMED_OUT``, create/reuse an active
    remote task (a fresh task restores the full retry-attempt budget) and move the
    item back to a live remote state, then recompute its job so it leaves the
    terminal state. Items no longer timed out are skipped; items rejected by the
    daily remote budget are skipped and counted (never silently dropped).
    """
    # Imported lazily to avoid an import cycle (services imports batches models).
    from apps.batches.services import (
        _enroll_remote_item,
        recompute_job_counters,
    )
    from apps.remote.budget import RemoteBudgetExceededError, normalize_principal
    from apps.remote.queue import RemoteQueueBackpressure

    selected_ids = list(item_ids)
    requeued = 0
    skipped_not_timed_out = 0
    skipped_budget = 0
    skipped_backpressure = 0
    deferred = 0
    affected_job_ids: set[uuid.UUID] = set()
    budget_exhausted_principals: set[str] = set()
    queue_backpressured = False

    if len(selected_ids) > REQUEUE_TIMED_OUT_INLINE_LIMIT:
        deferred, skipped_not_timed_out, affected_job_ids = _bulk_defer_timed_out_items(
            selected_ids
        )
        _release_jobs_for_remote_retry(affected_job_ids)
        for job_id in affected_job_ids:
            recompute_job_counters(job_id)
        result = JobAdminOperationResult(
            items_deferred=deferred,
            items_skipped_not_timed_out=skipped_not_timed_out,
        )
        _audit(
            "admin_requeue_timed_out",
            {
                "actor": actor,
                "items_requeued": result.items_requeued,
                "items_deferred": result.items_deferred,
                "items_skipped_not_timed_out": result.items_skipped_not_timed_out,
                "items_skipped_budget": result.items_skipped_budget,
                "items_skipped_backpressure": result.items_skipped_backpressure,
                "job_ids": _job_id_strings(affected_job_ids),
            },
        )
        return result

    for chunk_ids in _chunks(selected_ids, REQUEUE_TIMED_OUT_CHUNK_SIZE):
        chunk_affected_job_ids: set[uuid.UUID] = set()
        chunk_release_job_ids: set[uuid.UUID] = set()
        with transaction.atomic():
            # Lock only BatchItem rows; compound is nullable and joins with LEFT OUTER.
            items = list(
                BatchItem.objects.select_for_update(of=("self",))
                .select_related("job", "compound")
                .filter(pk__in=chunk_ids)
            )
            retryable_items = [
                item
                for item in items
                if item.status == BatchItemStatus.REMOTE_TIMED_OUT and item.inchikey
            ]
            skipped_not_timed_out += len(items) - len(retryable_items)
            retryable_inchikeys = {
                str(item.inchikey)
                for item in retryable_items
            }
            active_tasks = {
                task.inchikey: task
                for task in RemoteTask.objects.filter(
                    inchikey__in=retryable_inchikeys,
                    state__in=ACTIVE_TASK_STATES,
                )
            }

            backpressured_item_ids: set[int] = set()
            items_requiring_new_tasks = [
                item
                for item in retryable_items
                if item.inchikey not in active_tasks
            ]
            if items_requiring_new_tasks and (
                queue_backpressured or _remote_queue_at_capacity()
            ):
                queue_backpressured = True
                updated, job_ids, item_ids = _bulk_mark_backpressured(
                    items_requiring_new_tasks
                )
                skipped_backpressure += updated
                chunk_affected_job_ids.update(job_ids)
                chunk_release_job_ids.update(job_ids)
                backpressured_item_ids.update(item_ids)

            for index, item in enumerate(retryable_items):
                if item.pk in backpressured_item_ids:
                    continue
                job = item.job
                if item.inchikey not in active_tasks:
                    principal = normalize_principal(job.remote_principal)
                    if principal in budget_exhausted_principals:
                        skipped_budget += 1
                        continue
                try:
                    item.status = _enroll_remote_item(
                        item,
                        item.compound,
                        principal=job.remote_principal,
                        priority=job.priority,
                    )
                except RemoteBudgetExceededError:
                    budget_exhausted_principals.add(
                        normalize_principal(job.remote_principal)
                    )
                    skipped_budget += 1
                    continue
                except RemoteQueueBackpressure:
                    queue_backpressured = True
                    remaining_without_tasks = [
                        candidate
                        for candidate in retryable_items[index:]
                        if candidate.pk not in backpressured_item_ids
                        and candidate.inchikey not in active_tasks
                    ]
                    updated, job_ids, item_ids = _bulk_mark_backpressured(
                        remaining_without_tasks
                    )
                    skipped_backpressure += updated
                    chunk_affected_job_ids.update(job_ids)
                    chunk_release_job_ids.update(job_ids)
                    backpressured_item_ids.update(item_ids)
                    continue
                if item.remote_task is not None:
                    active_tasks[item.inchikey] = item.remote_task
                item.save(
                    update_fields=[
                        "status",
                        "remote_task",
                        "message",
                        "classification_snapshot",
                        "resolved_classification",
                    ]
                )
                requeued += 1
                chunk_affected_job_ids.add(job.pk)

            _release_jobs_for_remote_retry(chunk_release_job_ids)
            for job_id in chunk_affected_job_ids:
                recompute_job_counters(job_id)
        affected_job_ids.update(chunk_affected_job_ids)

    result = JobAdminOperationResult(
        items_requeued=requeued,
        items_deferred=deferred,
        items_skipped_not_timed_out=skipped_not_timed_out,
        items_skipped_budget=skipped_budget,
        items_skipped_backpressure=skipped_backpressure,
    )
    _audit(
        "admin_requeue_timed_out",
        {
            "actor": actor,
            "items_requeued": result.items_requeued,
            "items_deferred": result.items_deferred,
            "items_skipped_not_timed_out": result.items_skipped_not_timed_out,
            "items_skipped_budget": result.items_skipped_budget,
            "items_skipped_backpressure": result.items_skipped_backpressure,
            "job_ids": _job_id_strings(affected_job_ids),
        },
    )
    return result


def _bulk_defer_budget_delayed_items(
    item_ids: Sequence[object],
) -> tuple[int, int, set[uuid.UUID]]:
    """Hand a large budget-delayed requeue to the ingest worker.

    The rows are already the worker's durable retry record; all a big selection
    needs is to make their jobs claimable NOW instead of at the parked gate
    (next budget window). Collect the affected jobs across all chunks first, then
    clear their claim + gate ONCE at the end so a job released by an early chunk
    cannot be re-claimed by a worker and have that fresh claim clobbered by a
    later chunk's release (mirrors the timed-out bulk path's transform-then-
    release ordering). No task is enqueued synchronously; the worker re-probes.

    Returns ``(deferred, skipped_not_budget_delayed, affected_job_ids)``.
    """
    deferred = 0
    skipped_not_budget_delayed = 0
    affected_job_ids: set[uuid.UUID] = set()

    for chunk_ids in _chunks(item_ids, REQUEUE_TIMED_OUT_CHUNK_SIZE):
        existing = BatchItem.objects.filter(pk__in=chunk_ids).count()
        rows = list(
            BatchItem.objects.filter(
                pk__in=chunk_ids,
                status=BatchItemStatus.REMOTE_DELAYED,
                message=BUDGET_EXCEEDED_MESSAGE,
                inchikey__isnull=False,
            )
            .exclude(inchikey="")
            .values_list("pk", "job_id")
        )
        deferred += len(rows)
        skipped_not_budget_delayed += existing - len(rows)
        affected_job_ids.update(job_id for _pk, job_id in rows)

    _release_jobs_for_remote_retry(affected_job_ids)
    return deferred, skipped_not_budget_delayed, affected_job_ids


def requeue_budget_delayed_items(
    item_ids: Iterable[object],
    *,
    actor: str,
) -> JobAdminOperationResult:
    """Re-queue REMOTE_DELAYED items parked by the daily remote budget.

    The ingest worker already drains these on its own once ``retry_not_before``
    (the next budget window) elapses. This is the operator's "now" button for
    when the budget was just raised and waiting out that gate is not acceptable.

    Bounded like :func:`requeue_timed_out_items`: selections over
    ``REQUEUE_TIMED_OUT_INLINE_LIMIT`` are handed to the worker (claim + gate
    cleared) rather than enqueued synchronously in the admin request. Smaller
    selections enqueue inline for immediate feedback; rows still denied by the
    budget stay REMOTE_DELAYED and are counted (never silently dropped), and on
    the first queue-capacity denial the remaining rows are deferred rather than
    re-probed one by one. Every touched job is released (claim + gate cleared) so
    whatever this pass could not enqueue is retried promptly by the worker.
    """
    # Imported lazily to avoid an import cycle (services imports batches models).
    from apps.batches.services import (
        _enroll_remote_item,
        recompute_job_counters,
    )
    from apps.remote.budget import RemoteBudgetExceededError, normalize_principal
    from apps.remote.queue import RemoteQueueBackpressure

    selected_ids = list(item_ids)

    if len(selected_ids) > REQUEUE_TIMED_OUT_INLINE_LIMIT:
        deferred, skipped_not_budget_delayed, affected_job_ids = (
            _bulk_defer_budget_delayed_items(selected_ids)
        )
        for job_id in affected_job_ids:
            recompute_job_counters(job_id)
        result = JobAdminOperationResult(
            items_deferred=deferred,
            items_skipped_not_budget_delayed=skipped_not_budget_delayed,
        )
        _audit(
            "admin_requeue_budget_delayed",
            {
                "actor": actor,
                "items_deferred": result.items_deferred,
                "items_skipped_not_budget_delayed": result.items_skipped_not_budget_delayed,
                "job_ids": _job_id_strings(affected_job_ids),
            },
        )
        return result

    requeued = 0
    skipped_not_budget_delayed = 0
    skipped_budget = 0
    skipped_backpressure = 0
    affected_job_ids = set()
    budget_exhausted_principals: set[str] = set()

    for chunk_ids in _chunks(selected_ids, REQUEUE_TIMED_OUT_CHUNK_SIZE):
        with transaction.atomic():
            # Lock only BatchItem rows; compound is nullable and joins with LEFT OUTER.
            items = list(
                BatchItem.objects.select_for_update(of=("self",))
                .select_related("job", "compound")
                .filter(pk__in=chunk_ids)
            )
            retryable_items = [
                item
                for item in items
                if item.status == BatchItemStatus.REMOTE_DELAYED
                and item.message == BUDGET_EXCEEDED_MESSAGE
                and item.inchikey
            ]
            skipped_not_budget_delayed += len(items) - len(retryable_items)
            # Release every touched job regardless of per-row outcome: a still-
            # budget-denied row re-parks on the next worker pass, but a row now
            # blocked only by backpressure must not stay behind the old (next-
            # midnight) budget gate.
            chunk_affected_job_ids = {item.job_id for item in retryable_items}

            for index, item in enumerate(retryable_items):
                principal = normalize_principal(item.job.remote_principal)
                # Every row in a job shares one principal, so once it is denied no
                # later row of that principal can succeed: stop spending probes.
                if principal in budget_exhausted_principals:
                    skipped_budget += 1
                    continue
                try:
                    item.status = _enroll_remote_item(
                        item,
                        item.compound,
                        principal=item.job.remote_principal,
                        priority=item.job.priority,
                    )
                except RemoteBudgetExceededError:
                    budget_exhausted_principals.add(principal)
                    skipped_budget += 1
                    continue
                except RemoteQueueBackpressure:
                    # Capacity is a GLOBAL condition: no remaining row can enqueue
                    # this pass either. Re-label this row + the rest queue-capacity
                    # so their delay-cause UI reflects the CURRENT blocker instead
                    # of the stale budget message, then stop probing; the released
                    # jobs' next worker pass retries them (and re-labels any that
                    # turn out to be still budget-blocked).
                    remaining = retryable_items[index:]
                    BatchItem.objects.filter(
                        pk__in=[row.pk for row in remaining],
                        status=BatchItemStatus.REMOTE_DELAYED,
                    ).update(
                        remote_task=None,
                        message=REMOTE_QUEUE_BACKPRESSURE_MESSAGE,
                    )
                    skipped_backpressure += len(remaining)
                    break
                item.save(
                    update_fields=[
                        "status",
                        "remote_task",
                        "message",
                        "classification_snapshot",
                        "resolved_classification",
                    ]
                )
                requeued += 1

            _release_jobs_for_remote_retry(chunk_affected_job_ids)
            for job_id in chunk_affected_job_ids:
                recompute_job_counters(job_id)
        affected_job_ids.update(chunk_affected_job_ids)

    result = JobAdminOperationResult(
        items_requeued=requeued,
        items_skipped_not_budget_delayed=skipped_not_budget_delayed,
        items_skipped_budget=skipped_budget,
        items_skipped_backpressure=skipped_backpressure,
    )
    _audit(
        "admin_requeue_budget_delayed",
        {
            "actor": actor,
            "items_requeued": result.items_requeued,
            "items_skipped_not_budget_delayed": result.items_skipped_not_budget_delayed,
            "items_skipped_budget": result.items_skipped_budget,
            "items_skipped_backpressure": result.items_skipped_backpressure,
            "job_ids": _job_id_strings(affected_job_ids),
        },
    )
    return result
