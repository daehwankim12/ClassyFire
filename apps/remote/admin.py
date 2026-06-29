"""Admin registration for the remote work queue and its control surface.

All models are read-only. Three audited operational actions are exposed:

* RemoteTask  — reset selected DEFERRED/RETRY_WAIT tasks to READY.
* RemoteProviderControl — re-enable an AUTH_BLOCKED provider, plus global
  pause / resume buttons (with a live status banner) on the changelist.
* RemoteSubmission — mark an in-doubt UNKNOWN submission FAILED.

Every action reuses verified service logic and emits a SystemEvent audit row.
"""
from __future__ import annotations

from typing import Any

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import path
from django.utils import timezone

from apps.core.admin_base import ReadOnlyModelAdmin
from apps.core.enums import EventCategory, SubmissionState, TaskState
from apps.operations.admin_views import is_remote_paused, pause_remote, resume_remote
from apps.operations.models import SystemEvent
from apps.remote.models import (
    RemoteAttempt,
    RemoteBudgetLedger,
    RemoteCircuit,
    RemotePrincipalState,
    RemoteProviderControl,
    RemoteQuery,
    RemoteQueryItem,
    RemoteQueueControl,
    RemoteRateBucket,
    RemoteSubmission,
    RemoteSubmissionMember,
    RemoteTask,
)
from apps.remote.services import fail_unknown_submission, reset_provider_auth

# States safe to force back to READY: parked, not held by any worker. In-flight
# states (LOCKED/RUNNING/ENTITY_LOOKUP/SUBMIT_WAIT/SUBMITTED/POLL_WAIT) and the
# terminal DONE are deliberately excluded so a reset never races a live worker.
_RETRYABLE_TASK_STATES = (TaskState.DEFERRED, TaskState.RETRY_WAIT)


@admin.register(RemoteTask)
class RemoteTaskAdmin(ReadOnlyModelAdmin):
    list_display = (
        "inchikey",
        "state",
        "priority",
        "principal",
        "attempt",
        "next_run_at",
        "lock_owner",
        "updated_at",
    )
    list_filter = ("state", "principal")
    search_fields = ("inchikey", "lock_owner", "principal")
    actions = ("reset_to_ready",)

    @admin.action(
        description="Reset selected DEFERRED/RETRY_WAIT tasks to READY",
        permissions=("operate",),
    )
    def reset_to_ready(
        self, request: HttpRequest, queryset: QuerySet[RemoteTask]
    ) -> None:
        now = timezone.now()
        count = queryset.filter(state__in=_RETRYABLE_TASK_STATES).update(
            state=TaskState.READY,
            next_run_at=now,
            lock_owner=None,
            locked_at=None,
        )
        SystemEvent.objects.create(
            category=EventCategory.WORKER,
            event_type="admin_retry_tasks",
            payload={"count": count, "actor": request.user.get_username()},
        )
        self.message_user(
            request,
            f"Reset {count} task(s) to READY (in-flight / DONE skipped).",
        )


@admin.register(RemoteProviderControl)
class RemoteProviderControlAdmin(ReadOnlyModelAdmin):
    change_list_template = "admin/remote/remoteprovidercontrol/change_list.html"
    list_display = (
        "provider",
        "state",
        "blocked_reason",
        "credential_version",
        "updated_at",
    )
    list_filter = ("state",)
    actions = ("reset_auth",)

    @admin.action(
        description="Reset provider auth (re-enable)",
        permissions=("operate",),
    )
    def reset_auth(
        self, request: HttpRequest, queryset: QuerySet[RemoteProviderControl]
    ) -> None:
        actor = request.user.get_username()
        providers = list(queryset.values_list("provider", flat=True))
        for provider in providers:
            reset_provider_auth(provider=provider, actor=actor)
        self.message_user(request, f"Re-enabled {len(providers)} provider(s).")

    # --- global remote pause / resume (changelist buttons) ---------------
    def get_urls(self) -> list[Any]:
        custom = [
            path(
                "pause-remote/",
                self.admin_site.admin_view(self.pause_remote_view),
                name="remote_remoteprovidercontrol_pause",
            ),
            path(
                "resume-remote/",
                self.admin_site.admin_view(self.resume_remote_view),
                name="remote_remoteprovidercontrol_resume",
            ),
        ]
        return custom + super().get_urls()

    def changelist_view(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        extra_context = {
            **(extra_context or {}),
            "is_remote_paused": is_remote_paused(),
            "can_operate": self.has_operate_permission(request),
        }
        return super().changelist_view(request, extra_context=extra_context)

    def pause_remote_view(self, request: HttpRequest) -> HttpResponse:
        # admin_view only checks is_staff; the global pause/resume toggle is an
        # operator capability, so enforce the same gate as the actions.
        if not self.has_operate_permission(request):
            raise PermissionDenied
        if request.method == "POST":
            pause_remote(actor=request.user.get_username())
            self.message_user(
                request, "Remote task claiming paused.", level=messages.WARNING
            )
        return HttpResponseRedirect("../")

    def resume_remote_view(self, request: HttpRequest) -> HttpResponse:
        if not self.has_operate_permission(request):
            raise PermissionDenied
        if request.method == "POST":
            resume_remote(actor=request.user.get_username())
            self.message_user(request, "Remote task claiming resumed.")
        return HttpResponseRedirect("../")


@admin.register(RemoteSubmission)
class RemoteSubmissionAdmin(ReadOnlyModelAdmin):
    list_display = (
        "id",
        "provider",
        "state",
        "remote_query_id",
        "reconcile_attempts",
        "created_at",
    )
    list_filter = ("state", "provider")
    search_fields = ("payload_hash", "remote_query_id")
    actions = ("mark_failed",)

    @admin.action(
        description="Mark UNKNOWN submission as FAILED (free idempotency key)",
        permissions=("operate",),
    )
    def mark_failed(
        self, request: HttpRequest, queryset: QuerySet[RemoteSubmission]
    ) -> None:
        actor = request.user.get_username()
        selected_ids = list(queryset.values_list("pk", flat=True))
        unknown = list(
            RemoteSubmission.objects.filter(
                pk__in=selected_ids, state=SubmissionState.UNKNOWN
            )
        )
        for submission in unknown:
            fail_unknown_submission(submission, actor=actor)
        skipped = len(selected_ids) - len(unknown)
        self.message_user(
            request,
            f"Marked {len(unknown)} UNKNOWN submission(s) FAILED; "
            f"skipped {skipped} non-UNKNOWN.",
            level=messages.WARNING if skipped else messages.INFO,
        )


@admin.register(RemoteQuery)
class RemoteQueryAdmin(ReadOnlyModelAdmin):
    list_display = (
        "id",
        "provider",
        "remote_query_id",
        "state",
        "submitted_at",
        "completed_at",
    )
    list_filter = ("state", "provider")
    search_fields = ("remote_query_id",)


@admin.register(RemoteQueryItem)
class RemoteQueryItemAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "query", "task", "inchikey", "apply_state", "applied_at")
    list_filter = ("apply_state",)
    search_fields = ("inchikey",)
    list_select_related = ("query", "task")


@admin.register(RemoteSubmissionMember)
class RemoteSubmissionMemberAdmin(ReadOnlyModelAdmin):
    list_display = (
        "id",
        "submission",
        "provider",
        "inchikey",
        "active",
        "released_at",
        "release_reason",
    )
    list_filter = ("active", "provider")
    search_fields = ("inchikey",)
    list_select_related = ("submission",)


@admin.register(RemoteAttempt)
class RemoteAttemptAdmin(ReadOnlyModelAdmin):
    list_display = (
        "id",
        "operation",
        "classification",
        "provider",
        "http_status",
        "request_inchikey",
        "returned_inchikey",
        "created_at",
    )
    list_filter = ("operation", "classification", "provider")
    search_fields = ("request_inchikey", "returned_inchikey", "remote_query_id")
    list_select_related = ("query", "task")


@admin.register(RemoteCircuit)
class RemoteCircuitAdmin(ReadOnlyModelAdmin):
    list_display = (
        "provider",
        "state",
        "consecutive_failures",
        "opened_at",
        "updated_at",
    )
    list_filter = ("state",)


@admin.register(RemoteRateBucket)
class RemoteRateBucketAdmin(ReadOnlyModelAdmin):
    list_display = (
        "provider",
        "bucket",
        "capacity",
        "tokens",
        "refill_rate_per_sec",
        "blocked_until",
    )
    list_filter = ("provider",)


@admin.register(RemoteBudgetLedger)
class RemoteBudgetLedgerAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "principal", "units", "reason", "task", "created_at")
    list_filter = ("reason",)
    search_fields = ("principal",)


@admin.register(RemotePrincipalState)
class RemotePrincipalStateAdmin(ReadOnlyModelAdmin):
    list_display = ("principal", "last_claimed_at", "claim_count", "updated_at")
    search_fields = ("principal",)


@admin.register(RemoteQueueControl)
class RemoteQueueControlAdmin(ReadOnlyModelAdmin):
    list_display = ("provider", "updated_at")
