"""Admin registration for batch jobs, items, quota events, and exports.

All read-only. ``BatchJobAdmin`` exposes one audited operational action —
recomputing the denormalised job counters from the item rows.
"""
from __future__ import annotations

from django.contrib import admin
from django.db.models import Model, QuerySet
from django.http import HttpRequest

from apps.batches.models import BatchItem, BatchJob, BatchQuotaEvent, ExportArtifact
from apps.core.admin_base import ReadOnlyModelAdmin, UuidSearchMixin
from apps.core.enums import EventCategory
from apps.operations.models import SystemEvent


@admin.register(BatchJob)
class BatchJobAdmin(UuidSearchMixin, ReadOnlyModelAdmin):
    uuid_search_field = "id"
    list_display = (
        "id",
        "status",
        "processing_state",
        "total_count",
        "completed_count",
        "remote_enabled",
        "created_at",
        "completed_at",
    )
    list_filter = ("status", "processing_state", "remote_enabled")
    search_fields = ("claimed_by", "remote_principal")
    actions = ("recompute_counters",)

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


@admin.register(BatchItem)
class BatchItemAdmin(UuidSearchMixin, ReadOnlyModelAdmin):
    uuid_search_field = "job_id"
    list_display = ("job", "row_order", "detected_type", "status", "inchikey")
    list_filter = ("status", "detected_type")
    search_fields = ("inchikey", "external_id")
    list_select_related = ("job",)


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
