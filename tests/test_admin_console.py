"""Tests for the read-only Django admin operations console.

Covers: every target model is registered and read-only; each changelist loads;
the curated operational actions perform the right (and only the right) state
transitions and emit SystemEvent audit rows; immutability is enforced.
"""
from __future__ import annotations

import pytest
from django.contrib import admin
from django.urls import reverse
from django.utils import timezone

from apps.batches.models import BatchItem, BatchJob, BatchQuotaEvent, ExportArtifact
from apps.chemont.models import ChemOntNode
from apps.classifications.models import (
    ClassificationHistory,
    ClassificationResult,
    ClassyfireLive,
    SourceArtifact,
)
from apps.compounds.models import CompoundStructure, CompoundUsage
from apps.core.enums import ProviderControlState, SubmissionState, TaskState
from apps.core.models import LoginThrottle
from apps.operations.admin_views import is_remote_paused
from apps.operations.models import AuditSignature, SystemEvent
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

pytestmark = pytest.mark.django_db(transaction=True)

# Every project model that must appear in the admin console.
REGISTERED_MODELS = [
    BatchJob, BatchItem, BatchQuotaEvent, ExportArtifact,
    RemoteTask, RemoteQuery, RemoteQueryItem, RemoteSubmission,
    RemoteSubmissionMember, RemoteAttempt, RemoteCircuit, RemoteRateBucket,
    RemoteProviderControl, RemoteBudgetLedger, RemotePrincipalState,
    RemoteQueueControl,
    ClassificationResult, ClassyfireLive, ClassificationHistory,
    SourceArtifact,
    SystemEvent, AuditSignature,
    CompoundStructure, CompoundUsage,
    ChemOntNode,
    LoginThrottle,
]

IK1 = "AAAAAAAAAAAAAA-BBBBBBBBBB-N"
IK2 = "CCCCCCCCCCCCCC-DDDDDDDDDD-N"
IK3 = "EEEEEEEEEEEEEE-FFFFFFFFFF-N"


# --------------------------------------------------------------------------
# Registration & read-only contract
# --------------------------------------------------------------------------

def test_all_target_models_registered():
    for model in REGISTERED_MODELS:
        assert model in admin.site._registry, f"{model.__name__} not registered"


def test_all_registered_admins_are_read_only(rf, django_user_model):
    request = rf.get("/admin/")
    request.user = django_user_model(is_superuser=True, is_staff=True)
    for model in REGISTERED_MODELS:
        model_admin = admin.site._registry[model]
        assert model_admin.has_add_permission(request) is False
        assert model_admin.has_change_permission(request) is False
        assert model_admin.has_delete_permission(request) is False


@pytest.mark.parametrize("model", REGISTERED_MODELS)
def test_changelist_loads(admin_client, model):
    url = reverse(
        f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist"
    )
    response = admin_client.get(url)
    assert response.status_code == 200


# --------------------------------------------------------------------------
# UUID search mixin
# --------------------------------------------------------------------------


def test_export_artifact_admin_does_not_expose_capability_token(admin_client):
    job = BatchJob.objects.create(secret_hash="x")
    artifact = ExportArtifact.objects.create(
        job=job,
        fmt="csv",
        token="live-export-token-secret",
        path="/tmp/live-export-token-secret.csv",
        checksum="a" * 64,
        byte_size=10,
        rows_signature="b" * 64,
        row_count=1,
        job_state_signature="c" * 64,
        expires_at=timezone.now() + timezone.timedelta(hours=1),
    )

    changelist = admin_client.get(reverse("admin:batches_exportartifact_changelist"))
    assert changelist.status_code == 200
    assert b"live-export-token-secret" not in changelist.content

    detail = admin_client.get(
        reverse("admin:batches_exportartifact_change", args=[artifact.pk])
    )
    assert detail.status_code == 200
    assert b"live-export-token-secret" not in detail.content

def test_batchjob_uuid_search_and_garbage_term(admin_client):
    job = BatchJob.objects.create(secret_hash="x")
    url = reverse("admin:batches_batchjob_changelist")
    assert admin_client.get(url, {"q": str(job.id)}).status_code == 200
    # A non-UUID term must not raise (falls through to text search_fields).
    assert admin_client.get(url, {"q": "not-a-uuid"}).status_code == 200


def test_uuid_search_uses_scoped_queryset_not_default_manager(rf, django_user_model):
    """UUID search must OR into the passed (filtered) queryset, not the default
    manager — otherwise it surfaces rows the active filter excluded."""
    from apps.core.enums import BatchJobStatus

    pending = BatchJob.objects.create(secret_hash="x", status=BatchJobStatus.PENDING)
    complete = BatchJob.objects.create(secret_hash="x", status=BatchJobStatus.COMPLETE)
    model_admin = admin.site._registry[BatchJob]
    request = rf.get("/admin/")
    request.user = django_user_model(is_superuser=True, is_staff=True)

    scoped = BatchJob.objects.filter(status=BatchJobStatus.PENDING)
    qs, _ = model_admin.get_search_results(request, scoped, str(complete.id))
    # complete is excluded by the scoped queryset → must NOT be surfaced.
    assert complete.id not in set(qs.values_list("id", flat=True))
    # A UUID within scope is still found.
    qs2, _ = model_admin.get_search_results(request, scoped, str(pending.id))
    assert pending.id in set(qs2.values_list("id", flat=True))


# --------------------------------------------------------------------------
# Operate permission gates the mutating actions / pause-resume views
# --------------------------------------------------------------------------

OPERATE_GATED_ACTIONS = [
    (RemoteTask, "reset_to_ready"),
    (RemoteProviderControl, "reset_auth"),
    (RemoteSubmission, "mark_failed"),
    (BatchJob, "recompute_counters"),
]


def test_operate_actions_hidden_from_non_superuser(rf, django_user_model):
    su_request = rf.get("/admin/")
    su_request.user = django_user_model(is_superuser=True, is_staff=True, is_active=True)
    view_request = rf.get("/admin/")
    view_request.user = django_user_model(
        is_superuser=False, is_staff=True, is_active=True
    )
    for model, action_name in OPERATE_GATED_ACTIONS:
        model_admin = admin.site._registry[model]
        assert model_admin.has_operate_permission(su_request) is True
        assert model_admin.has_operate_permission(view_request) is False
        # Gated action visible to a superuser, filtered out for a view-only staff.
        assert action_name in model_admin.get_actions(su_request)
        assert action_name not in model_admin.get_actions(view_request)


def test_pause_resume_views_forbidden_for_non_superuser(client, django_user_model):
    staff = django_user_model.objects.create_user(
        username="viewer", password="pw", is_staff=True
    )
    client.force_login(staff)
    pause_resp = client.post(reverse("admin:remote_remoteprovidercontrol_pause"))
    assert pause_resp.status_code == 403
    assert is_remote_paused() is False
    resume_resp = client.post(reverse("admin:remote_remoteprovidercontrol_resume"))
    assert resume_resp.status_code == 403


# --------------------------------------------------------------------------
# Operational action: recompute job counters
# --------------------------------------------------------------------------

def test_recompute_counters_action(admin_client):
    job = BatchJob.objects.create(secret_hash="x", total_count=999)
    url = reverse("admin:batches_batchjob_changelist")
    response = admin_client.post(
        url,
        {"action": "recompute_counters", "_selected_action": [str(job.pk)]},
    )
    assert response.status_code == 302
    assert SystemEvent.objects.filter(
        event_type="admin_recompute_counters"
    ).exists()


# --------------------------------------------------------------------------
# Operational action: retry DEFERRED/RETRY_WAIT tasks, skip in-flight
# --------------------------------------------------------------------------

def test_reset_tasks_action_only_touches_parked_states(admin_client):
    deferred = RemoteTask.objects.create(inchikey=IK1, state=TaskState.DEFERRED)
    retry_wait = RemoteTask.objects.create(inchikey=IK2, state=TaskState.RETRY_WAIT)
    running = RemoteTask.objects.create(inchikey=IK3, state=TaskState.RUNNING)

    url = reverse("admin:remote_remotetask_changelist")
    response = admin_client.post(
        url,
        {
            "action": "reset_to_ready",
            "_selected_action": [str(deferred.pk), str(retry_wait.pk), str(running.pk)],
        },
    )
    assert response.status_code == 302

    deferred.refresh_from_db()
    retry_wait.refresh_from_db()
    running.refresh_from_db()
    assert deferred.state == TaskState.READY
    assert retry_wait.state == TaskState.READY
    assert running.state == TaskState.RUNNING  # in-flight: untouched
    event = SystemEvent.objects.filter(event_type="admin_retry_tasks").latest("created_at")
    assert event.payload["count"] == 2


# --------------------------------------------------------------------------
# Service: reset provider auth (shared by command + admin action)
# --------------------------------------------------------------------------

def test_reset_provider_auth_reenables_and_audits():
    RemoteProviderControl.objects.create(
        provider="wishart",
        state=ProviderControlState.AUTH_BLOCKED,
        blocked_reason="401 from provider",
    )
    event = reset_provider_auth(provider="wishart", actor="tester")

    control = RemoteProviderControl.objects.get(provider="wishart")
    assert control.state == ProviderControlState.ENABLED
    assert control.blocked_reason == ""
    assert event.event_type == "provider_auth_reset"
    assert event.payload == {"provider": "wishart", "actor": "tester"}


# --------------------------------------------------------------------------
# Service: fail UNKNOWN submission (shared by command + admin action)
# --------------------------------------------------------------------------

def test_fail_unknown_submission_frees_key_and_releases_members():
    submission = RemoteSubmission.objects.create(
        provider="wishart",
        payload_hash="a" * 64,
        state=SubmissionState.UNKNOWN,
        request_payload={},
    )
    member = RemoteSubmissionMember.objects.create(
        submission=submission,
        provider="wishart",
        inchikey=IK1,
        structure_hash="b" * 64,
        active=True,
    )

    fail_unknown_submission(submission, actor="tester")

    submission.refresh_from_db()
    member.refresh_from_db()
    assert submission.state == SubmissionState.FAILED
    assert member.active is False
    assert member.release_reason == "operator_mark_failed"
    assert SystemEvent.objects.filter(event_type="submission_marked_failed").exists()


def test_fail_unknown_submission_refuses_non_unknown():
    submission = RemoteSubmission.objects.create(
        provider="wishart",
        payload_hash="c" * 64,
        state=SubmissionState.PENDING,
        request_payload={},
    )
    with pytest.raises(ValueError):
        fail_unknown_submission(submission, actor="tester")
    submission.refresh_from_db()
    assert submission.state == SubmissionState.PENDING  # untouched


def test_mark_failed_action_skips_non_unknown(admin_client):
    unknown = RemoteSubmission.objects.create(
        provider="wishart", payload_hash="d" * 64,
        state=SubmissionState.UNKNOWN, request_payload={},
    )
    pending = RemoteSubmission.objects.create(
        provider="wishart", payload_hash="e" * 64,
        state=SubmissionState.PENDING, request_payload={},
    )
    url = reverse("admin:remote_remotesubmission_changelist")
    response = admin_client.post(
        url,
        {
            "action": "mark_failed",
            "_selected_action": [str(unknown.pk), str(pending.pk)],
        },
    )
    assert response.status_code == 302
    unknown.refresh_from_db()
    pending.refresh_from_db()
    assert unknown.state == SubmissionState.FAILED
    assert pending.state == SubmissionState.PENDING  # skipped


# --------------------------------------------------------------------------
# Global remote pause / resume buttons
# --------------------------------------------------------------------------

def test_pause_then_resume_remote_buttons(admin_client):
    assert is_remote_paused() is False
    admin_client.post(reverse("admin:remote_remoteprovidercontrol_pause"))
    assert is_remote_paused() is True
    admin_client.post(reverse("admin:remote_remoteprovidercontrol_resume"))
    assert is_remote_paused() is False
