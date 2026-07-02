"""Admin registration for batch jobs, items, quota events, and exports.

Rows are inspection-only for add/change. Superusers can delete rows and run
curated operational actions such as counter recomputation and job cleanup.
"""
from __future__ import annotations

import uuid

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db.models import Model, QuerySet
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import URLPattern, path, reverse
from django.utils.html import format_html

from apps.batches.admin_ops import (
    cancel_and_delete_jobs,
    delete_jobs_with_artifacts,
    requeue_timed_out_items,
    set_job_priority,
)
from apps.batches.models import BatchItem, BatchJob, BatchQuotaEvent, ExportArtifact
from apps.core.admin_base import ReadOnlyModelAdmin, UuidSearchMixin
from apps.core.enums import EventCategory
from apps.core.security import set_job_access_cookie
from apps.operations.models import SystemEvent


@admin.register(BatchJob)
class BatchJobAdmin(UuidSearchMixin, ReadOnlyModelAdmin):
    uuid_search_field = "id"
    list_display = (
        "id",
        "status",
        "processing_state",
        "priority",
        "total_count",
        "completed_count",
        "remote_enabled",
        "created_at",
        "completed_at",
        "open_result_link",
    )
    list_filter = ("status", "processing_state", "remote_enabled", "priority")
    search_fields = ("claimed_by", "remote_principal")
    actions = (
        "recompute_counters",
        "cancel_and_delete",
        "set_high_priority",
        "reset_priority",
    )

    def get_urls(self) -> list[URLPattern]:
        custom: list[URLPattern] = [
            path(
                "<uuid:object_id>/open-result/",
                self.admin_site.admin_view(self.open_result_view),
                name="batches_batchjob_open_result",
            ),
        ]
        return custom + super().get_urls()

    @admin.display(description="Result")
    def open_result_link(self, obj: BatchJob) -> str:
        return format_html(
            '<a href="{}">Open result page</a>',
            reverse("admin:batches_batchjob_open_result", args=[obj.pk]),
        )

    def open_result_view(
        self,
        request: HttpRequest,
        object_id: uuid.UUID,
    ) -> HttpResponse:
        if not self.has_operate_permission(request):
            raise PermissionDenied
        job = self.get_object(request, str(object_id))
        if job is None:
            raise Http404("Batch job not found")
        response = HttpResponseRedirect(f"/jobs/{job.pk}/")
        set_job_access_cookie(response, request, job.pk)
        SystemEvent.objects.create(
            category=EventCategory.SECURITY,
            event_type="admin_open_job_result",
            payload={
                "actor": request.user.get_username(),
                "job_id": str(job.pk),
            },
        )
        return response

    def delete_queryset(
        self,
        request: HttpRequest,
        queryset: QuerySet[BatchJob],
    ) -> None:
        result = delete_jobs_with_artifacts(
            queryset.values_list("pk", flat=True),
            actor=request.user.get_username(),
        )
        self.message_user(request, f"Deleted {result.jobs_deleted} job(s).")

    def delete_model(self, request: HttpRequest, obj: BatchJob) -> None:
        delete_jobs_with_artifacts([obj.pk], actor=request.user.get_username())

    @admin.action(
        description="Recompute job counters from item rows",
        permissions=("operate",),
    )
    def recompute_counters(
        self, request: HttpRequest, queryset: QuerySet[BatchJob]
    ) -> None:
        from apps.batches.services import recompute_job_counters

        job_ids = [str(pk) for pk in queryset.values_list("id", flat=True)]
        for job_id in job_ids:
            recompute_job_counters(job_id)
        SystemEvent.objects.create(
            category=EventCategory.WORKER,
            event_type="admin_recompute_counters",
            payload={
                "count": len(job_ids),
                "actor": request.user.get_username(),
                "job_ids": job_ids,
            },
        )
        self.message_user(
            request, f"Recomputed counters for {len(job_ids)} job(s)."
        )

    @admin.action(
        description="Cancel and delete selected jobs",
        permissions=("operate",),
    )
    def cancel_and_delete(
        self, request: HttpRequest, queryset: QuerySet[BatchJob]
    ) -> None:
        result = cancel_and_delete_jobs(
            queryset.values_list("pk", flat=True),
            actor=request.user.get_username(),
        )
        self.message_user(
            request,
            (
                f"Deleted {result.jobs_deleted} job(s); "
                f"deleted {result.remote_tasks_deleted} remote task(s); "
                f"skipped {result.remote_tasks_skipped_shared} shared and "
                f"{result.remote_tasks_skipped_leased} leased task(s)."
            ),
        )

    @admin.action(
        description="Set selected jobs high priority",
        permissions=("operate",),
    )
    def set_high_priority(
        self, request: HttpRequest, queryset: QuerySet[BatchJob]
    ) -> None:
        result = set_job_priority(
            queryset.values_list("pk", flat=True),
            priority=100,
            actor=request.user.get_username(),
        )
        self.message_user(
            request,
            (
                f"Set {result.jobs_updated} job(s) to high priority; "
                f"updated {result.remote_tasks_updated} remote task(s)."
            ),
        )

    @admin.action(
        description="Reset selected jobs to normal priority",
        permissions=("operate",),
    )
    def reset_priority(
        self, request: HttpRequest, queryset: QuerySet[BatchJob]
    ) -> None:
        result = set_job_priority(
            queryset.values_list("pk", flat=True),
            priority=0,
            actor=request.user.get_username(),
        )
        self.message_user(
            request,
            (
                f"Reset {result.jobs_updated} job(s) to normal priority; "
                f"updated {result.remote_tasks_updated} remote task(s)."
            ),
        )


@admin.register(BatchItem)
class BatchItemAdmin(UuidSearchMixin, ReadOnlyModelAdmin):
    uuid_search_field = "job_id"
    list_display = ("job", "row_order", "detected_type", "status", "inchikey")
    list_filter = ("status", "detected_type")
    search_fields = ("inchikey", "external_id")
    list_select_related = ("job",)
    actions = ("retry_timed_out",)

    @admin.action(
        description="Retry timed-out remote items (re-queue)",
        permissions=("operate",),
    )
    def retry_timed_out(
        self, request: HttpRequest, queryset: QuerySet[BatchItem]
    ) -> None:
        result = requeue_timed_out_items(
            queryset.values_list("pk", flat=True),
            actor=request.user.get_username(),
        )
        self.message_user(
            request,
            (
                f"Re-queued {result.items_requeued} item(s); "
                f"skipped {result.items_skipped_not_timed_out} not-timed-out and "
                f"{result.items_skipped_budget} over-budget."
            ),
        )


@admin.register(BatchQuotaEvent)
class BatchQuotaEventAdmin(ReadOnlyModelAdmin):
    list_display = ("kind", "principal", "ip_address", "row_count", "created_at")
    list_filter = ("kind",)
    search_fields = ("principal", "ip_address")


@admin.register(ExportArtifact)
class ExportArtifactAdmin(UuidSearchMixin, ReadOnlyModelAdmin):
    uuid_search_field = "job_id"
    list_display = (
        "id",
        "job",
        "fmt",
        "byte_size",
        "row_count",
        "created_at",
        "expires_at",
    )
    list_filter = ("fmt",)
    search_fields = ("job__claimed_by", "job__remote_principal")
    list_select_related = ("job",)
    fields = (
        "id",
        "job",
        "fmt",
        "checksum",
        "byte_size",
        "rows_signature",
        "row_count",
        "job_state_signature",
        "created_at",
        "expires_at",
    )

    def get_readonly_fields(
        self, request: HttpRequest, obj: Model | None = None
    ) -> list[str]:
        # The export token is a bearer capability for /api/exports/<token>; do
        # not render or search it in the admin inspection surface.
        return list(self.fields)
