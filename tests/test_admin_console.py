"""Tests for the inspection-only Django admin operations console.

Covers: every target model is registered; add/change stay blocked; only
BatchJob exposes curated superuser deletion; each changelist loads; curated
operational actions perform the right state transitions and emit SystemEvent
audit rows.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.contrib import admin
from django.contrib.admin.options import ModelAdmin
from django.urls import reverse
from django.utils import timezone

from apps.batches import admin as batch_admin_module
from apps.batches.models import BatchItem, BatchJob, BatchQuotaEvent, ExportArtifact
from apps.chemont.models import ChemOntNode
from apps.classifications.models import (
    ClassificationHistory,
    ClassificationResult,
    ClassyfireLive,
    SourceArtifact,
)
from apps.compounds.models import CompoundStructure, CompoundUsage
from apps.core.enums import (
    BatchItemStatus,
    BatchJobStatus,
    BatchProcessingState,
    ProviderControlState,
    QueryState,
    SubmissionState,
    TaskState,
)
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


def _admin_user(*, is_superuser: bool) -> SimpleNamespace:
    return SimpleNamespace(
        is_active=True,
        is_staff=True,
        is_superuser=is_superuser,
        has_perm=lambda _perm: is_superuser,
    )


# --------------------------------------------------------------------------
# Registration & read-only contract
# --------------------------------------------------------------------------

def test_all_target_models_registered():
    for model in REGISTERED_MODELS:
        assert model in admin.site._registry, f"{model.__name__} not registered"


def test_all_registered_admins_block_add_and_change(rf):
    request = rf.get("/admin/")
    request.user = _admin_user(is_superuser=True)
    for model in REGISTERED_MODELS:
        model_admin = admin.site._registry[model]
        assert model_admin.has_add_permission(request) is False
        assert model_admin.has_change_permission(request) is False


def test_registered_admins_deny_native_delete_except_batchjob_cleanup(rf):
    super_request = rf.get("/admin/")
    super_request.user = _admin_user(is_superuser=True)
    staff_request = rf.get("/admin/")
    staff_request.user = _admin_user(is_superuser=False)
    for model in REGISTERED_MODELS:
        model_admin = admin.site._registry[model]
        expected_superuser_delete = model is BatchJob
        assert model_admin.has_delete_permission(super_request) is expected_superuser_delete
        assert model_admin.has_delete_permission(staff_request) is False


def test_batchjob_related_admins_allow_delete_only_inside_job_cascade(rf):
    request = rf.get("/admin/")
    request.user = _admin_user(is_superuser=True)
    attr = batch_admin_module._ALLOW_BATCH_JOB_CASCADE_DELETE_ATTR

    for model in (BatchItem, ExportArtifact):
        model_admin = admin.site._registry[model]
        assert model_admin.has_delete_permission(request) is False

        setattr(request, attr, True)
        assert model_admin.has_delete_permission(request) is True

        setattr(request, attr, False)
        assert model_admin.has_delete_permission(request) is False


def test_batchjob_delete_collector_scopes_related_delete_permission(rf, monkeypatch):
    request = rf.get("/admin/")
    request.user = _admin_user(is_superuser=True)
    attr = batch_admin_module._ALLOW_BATCH_JOB_CASCADE_DELETE_ATTR
    job_admin = admin.site._registry[BatchJob]
    seen_flags: list[bool] = []

    def fake_get_deleted_objects(self, objs, request):
        seen_flags.append(bool(getattr(request, attr, False)))
        return [], {}, set(), []

    monkeypatch.setattr(ModelAdmin, "get_deleted_objects", fake_get_deleted_objects)

    job_admin.get_deleted_objects([], request)

    assert seen_flags == [True]
    assert not hasattr(request, attr)


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


def test_claim_job_for_processing_prefers_higher_priority():
    from apps.batches.services import claim_job_for_processing

    low = BatchJob.objects.create(
        secret_hash="x",
        priority=0,
        processing_state=BatchProcessingState.PARSING,
    )
    high = BatchJob.objects.create(
        secret_hash="x",
        priority=100,
        processing_state=BatchProcessingState.PARSING,
    )

    claimed = claim_job_for_processing("worker-priority")

    assert claimed is not None
    assert claimed.pk == high.pk
    assert claimed.pk != low.pk


# --------------------------------------------------------------------------
# Operate permission gates the mutating actions / pause-resume views
# --------------------------------------------------------------------------

OPERATE_GATED_ACTIONS = [
    (RemoteTask, "reset_to_ready"),
    (RemoteProviderControl, "reset_auth"),
    (RemoteSubmission, "mark_failed"),
    (BatchJob, "recompute_counters"),
    (BatchJob, "cancel_and_delete"),
    (BatchJob, "set_high_priority"),
    (BatchJob, "reset_priority"),
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
# Batch job admin operations
# --------------------------------------------------------------------------


def test_delete_jobs_with_artifacts_removes_files(settings, tmp_path):
    from apps.batches.admin_ops import delete_jobs_with_artifacts

    upload_root = tmp_path / "uploads"
    export_root = tmp_path / "exports"
    upload_root.mkdir()
    export_root.mkdir()
    settings.UPLOAD_ARTIFACT_DIR = str(upload_root)
    settings.EXPORT_DIR = export_root

    upload_artifact = upload_root / "raw.csv"
    upload_artifact.write_text("CCO\n")
    export_payload = export_root / "export-token.csv"
    export_payload.write_text("row\n")
    export_sidecar = export_root / "export-token.meta.json"
    export_sidecar.write_text("{}")

    job = BatchJob.objects.create(
        secret_hash="x",
        status=BatchJobStatus.COMPLETE,
        upload_artifact_path=upload_artifact.name,
    )
    ExportArtifact.objects.create(
        job=job,
        fmt="csv",
        token="export-token",
        path=str(export_payload),
        checksum="a" * 64,
        byte_size=export_payload.stat().st_size,
        rows_signature="b" * 64,
        row_count=1,
        job_state_signature="c" * 64,
        expires_at=timezone.now() + timezone.timedelta(hours=1),
    )

    result = delete_jobs_with_artifacts([job.pk], actor="tester")

    assert result.jobs_deleted == 1
    assert result.upload_artifacts_deleted == 1
    assert result.export_files_deleted == 2
    assert not BatchJob.objects.filter(pk=job.pk).exists()
    assert not upload_artifact.exists()
    assert not export_payload.exists()
    assert not export_sidecar.exists()
    event = SystemEvent.objects.filter(event_type="admin_delete_jobs").latest("created_at")
    assert event.payload["actor"] == "tester"
    assert event.payload["jobs_deleted"] == 1


def test_cancel_and_delete_preserves_shared_remote_tasks():
    from apps.batches.admin_ops import cancel_and_delete_jobs

    selected = BatchJob.objects.create(secret_hash="x", priority=100)
    other = BatchJob.objects.create(secret_hash="x", priority=50)
    BatchItem.objects.create(
        job=selected,
        row_order=0,
        original_input=IK1,
        inchikey=IK1,
        status=BatchItemStatus.PENDING_CLASSIFICATION,
    )
    BatchItem.objects.create(
        job=selected,
        row_order=1,
        original_input=IK2,
        inchikey=IK2,
        status=BatchItemStatus.PENDING_CLASSIFICATION,
    )
    BatchItem.objects.create(
        job=selected,
        row_order=2,
        original_input=IK3,
        inchikey=IK3,
        status=BatchItemStatus.PENDING_CLASSIFICATION,
    )
    BatchItem.objects.create(
        job=other,
        row_order=0,
        original_input=IK2,
        inchikey=IK2,
        status=BatchItemStatus.PENDING_CLASSIFICATION,
    )
    unshared = RemoteTask.objects.create(inchikey=IK1, state=TaskState.READY)
    shared = RemoteTask.objects.create(inchikey=IK2, state=TaskState.READY, priority=100)
    leased = RemoteTask.objects.create(
        inchikey=IK3,
        state=TaskState.RUNNING,
        lock_owner="worker",
    )

    result = cancel_and_delete_jobs([selected.pk], actor="tester")

    assert result.jobs_deleted == 1
    assert result.remote_tasks_deleted == 1
    assert result.remote_tasks_skipped_shared == 1
    assert result.remote_tasks_skipped_leased == 1
    assert not BatchJob.objects.filter(pk=selected.pk).exists()
    assert not RemoteTask.objects.filter(pk=unshared.pk).exists()
    shared.refresh_from_db()
    assert shared.state == TaskState.READY
    assert shared.priority == other.priority
    leased.refresh_from_db()
    assert leased.state == TaskState.RUNNING
    event = SystemEvent.objects.filter(event_type="admin_cancel_delete_jobs").latest(
        "created_at"
    )
    assert event.payload["remote_tasks_deleted"] == 1


def test_cancel_and_delete_preserves_submitted_poll_wait_tasks():
    from apps.batches.admin_ops import cancel_and_delete_jobs

    selected = BatchJob.objects.create(secret_hash="x", priority=100)
    BatchItem.objects.create(
        job=selected,
        row_order=0,
        original_input=IK1,
        inchikey=IK1,
        status=BatchItemStatus.PENDING_CLASSIFICATION,
    )
    task = RemoteTask.objects.create(inchikey=IK1, state=TaskState.POLL_WAIT)
    query = RemoteQuery.objects.create(
        remote_query_id="admin-preserve-submitted",
        payload_hash="a" * 64,
        state=QueryState.SUBMITTED,
    )
    submission = RemoteSubmission.objects.create(
        payload_hash="a" * 64,
        state=SubmissionState.SUBMITTED,
        query=query,
        remote_query_id=query.remote_query_id,
        request_payload={"structures": [IK1]},
    )
    RemoteSubmissionMember.objects.create(
        submission=submission,
        inchikey=IK1,
        structure_hash="b" * 64,
    )
    query_item = RemoteQueryItem.objects.create(query=query, task=task, inchikey=IK1)

    result = cancel_and_delete_jobs([selected.pk], actor="tester")

    assert result.jobs_deleted == 1
    assert result.remote_tasks_deleted == 0
    assert result.remote_tasks_skipped_leased == 1
    task.refresh_from_db()
    assert task.state == TaskState.POLL_WAIT
    query_item.refresh_from_db()
    assert query_item.task_id == task.pk
    assert RemoteSubmissionMember.objects.filter(
        submission=submission, inchikey=IK1, active=True
    ).exists()


def test_set_job_priority_recomputes_remote_task_priority():
    from apps.batches.admin_ops import set_job_priority

    selected = BatchJob.objects.create(secret_hash="x", priority=0)
    other = BatchJob.objects.create(secret_hash="x", priority=50)
    BatchItem.objects.create(
        job=selected,
        row_order=0,
        original_input=IK1,
        inchikey=IK1,
        status=BatchItemStatus.PENDING_CLASSIFICATION,
    )
    BatchItem.objects.create(
        job=other,
        row_order=0,
        original_input=IK1,
        inchikey=IK1,
        status=BatchItemStatus.PENDING_CLASSIFICATION,
    )
    task = RemoteTask.objects.create(inchikey=IK1, state=TaskState.READY, priority=0)

    raised = set_job_priority([selected.pk], priority=100, actor="tester")
    selected.refresh_from_db()
    task.refresh_from_db()
    assert selected.priority == 100
    assert raised.remote_tasks_updated == 1
    assert task.priority == 100

    lowered = set_job_priority([selected.pk], priority=0, actor="tester")
    selected.refresh_from_db()
    task.refresh_from_db()
    assert selected.priority == 0
    assert lowered.remote_tasks_updated == 1
    assert task.priority == other.priority


def test_batchjob_admin_priority_actions(admin_client):
    job = BatchJob.objects.create(secret_hash="x", priority=0)
    url = reverse("admin:batches_batchjob_changelist")

    high = admin_client.post(
        url,
        {"action": "set_high_priority", "_selected_action": [str(job.pk)]},
    )
    assert high.status_code == 302
    job.refresh_from_db()
    assert job.priority == 100

    normal = admin_client.post(
        url,
        {"action": "reset_priority", "_selected_action": [str(job.pk)]},
    )
    assert normal.status_code == 302
    job.refresh_from_db()
    assert job.priority == 0
    assert SystemEvent.objects.filter(event_type="admin_set_job_priority").count() == 2


def test_batchjob_admin_cancel_and_delete_action(admin_client):
    job = BatchJob.objects.create(secret_hash="x")
    BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=IK1,
        inchikey=IK1,
        status=BatchItemStatus.PENDING_CLASSIFICATION,
    )
    task = RemoteTask.objects.create(inchikey=IK1, state=TaskState.READY)

    response = admin_client.post(
        reverse("admin:batches_batchjob_changelist"),
        {"action": "cancel_and_delete", "_selected_action": [str(job.pk)]},
    )

    assert response.status_code == 302
    assert not BatchJob.objects.filter(pk=job.pk).exists()
    assert not RemoteTask.objects.filter(pk=task.pk).exists()
    assert SystemEvent.objects.filter(event_type="admin_cancel_delete_jobs").exists()


def test_batchjob_admin_delete_view_allows_related_rows(admin_client):
    job = BatchJob.objects.create(secret_hash="x")
    item = BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=IK1,
        inchikey=IK1,
        status=BatchItemStatus.PENDING_CLASSIFICATION,
    )
    artifact = ExportArtifact.objects.create(
        job=job,
        fmt="csv",
        token="delete-view-token",
        path="/tmp/delete-view-token.csv",
        checksum="a" * 64,
        byte_size=10,
        rows_signature="b" * 64,
        row_count=1,
        job_state_signature="c" * 64,
        expires_at=timezone.now() + timezone.timedelta(hours=1),
    )
    url = reverse("admin:batches_batchjob_delete", args=[job.pk])

    confirmation = admin_client.get(url)

    assert confirmation.status_code == 200
    assert confirmation.context["perms_lacking"] == set()

    response = admin_client.post(url, {"post": "yes"})

    assert response.status_code == 302
    assert not BatchJob.objects.filter(pk=job.pk).exists()
    assert not BatchItem.objects.filter(pk=item.pk).exists()
    assert not ExportArtifact.objects.filter(pk=artifact.pk).exists()
    assert SystemEvent.objects.filter(event_type="admin_delete_jobs").exists()


def test_batchjob_admin_open_result_sets_cookie_and_redirects(admin_client):
    from apps.core.security import job_access_cookie_name

    job = BatchJob.objects.create(secret_hash="x")

    response = admin_client.get(
        reverse("admin:batches_batchjob_open_result", args=[job.pk])
    )

    assert response.status_code == 302
    assert response["Location"] == f"/jobs/{job.pk}/"
    assert job_access_cookie_name(job.pk) in response.cookies
    event = SystemEvent.objects.filter(event_type="admin_open_job_result").latest(
        "created_at"
    )
    assert event.payload["job_id"] == str(job.pk)


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


# --------------------------------------------------------------------------
# Retry timed-out remote items (re-queue)
# --------------------------------------------------------------------------


def test_requeue_timed_out_reactivates_item_and_job():
    from apps.batches.admin_ops import requeue_timed_out_items

    job = BatchJob.objects.create(
        secret_hash="x",
        status=BatchJobStatus.COMPLETE,
        total_count=1,
        invalid_count=1,
    )
    item = BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=IK1,
        inchikey=IK1,
        status=BatchItemStatus.REMOTE_TIMED_OUT,
    )

    result = requeue_timed_out_items([item.pk], actor="tester")

    assert result.items_requeued == 1
    assert result.items_skipped_not_timed_out == 0
    assert result.items_skipped_budget == 0
    item.refresh_from_db()
    assert item.status == BatchItemStatus.PENDING_CLASSIFICATION
    # A fresh active remote task now exists for the key.
    assert RemoteTask.objects.filter(
        inchikey=IK1, state__in=[s.value for s in TaskState.active_states()]
    ).exists()
    job.refresh_from_db()
    assert job.status == BatchJobStatus.IN_PROGRESS
    event = SystemEvent.objects.filter(event_type="admin_requeue_timed_out").latest(
        "created_at"
    )
    assert event.payload["items_requeued"] == 1
    assert event.category == "SECURITY"


def test_requeue_skips_non_timed_out_items():
    from apps.batches.admin_ops import requeue_timed_out_items

    job = BatchJob.objects.create(secret_hash="x", total_count=1, completed_count=1)
    item = BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=IK2,
        inchikey=IK2,
        status=BatchItemStatus.FOUND_LIVE_CACHE,
    )

    result = requeue_timed_out_items([item.pk], actor="tester")

    assert result.items_requeued == 0
    assert result.items_skipped_not_timed_out == 1
    item.refresh_from_db()
    assert item.status == BatchItemStatus.FOUND_LIVE_CACHE
    assert not RemoteTask.objects.filter(inchikey=IK2).exists()


def test_requeue_skips_over_budget_items(settings):
    from apps.batches.admin_ops import requeue_timed_out_items

    settings.REMOTE_DAILY_BUDGET_PER_PRINCIPAL = 0  # every new enqueue is over budget
    job = BatchJob.objects.create(
        secret_hash="x",
        status=BatchJobStatus.COMPLETE,
        total_count=1,
        invalid_count=1,
    )
    item = BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=IK3,
        inchikey=IK3,
        status=BatchItemStatus.REMOTE_TIMED_OUT,
    )

    result = requeue_timed_out_items([item.pk], actor="tester")

    assert result.items_requeued == 0
    assert result.items_skipped_budget == 1
    item.refresh_from_db()
    assert item.status == BatchItemStatus.REMOTE_TIMED_OUT  # unchanged
    assert not RemoteTask.objects.filter(inchikey=IK3).exists()


def test_requeue_parks_backpressure_items_and_audits(settings):
    from apps.batches.admin_ops import requeue_timed_out_items

    settings.REMOTE_MAX_QUEUE_DEPTH = 1
    RemoteTask.objects.create(inchikey=IK1, state=TaskState.READY)
    job = BatchJob.objects.create(
        secret_hash="x",
        status=BatchJobStatus.COMPLETE,
        total_count=1,
        invalid_count=1,
    )
    item = BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=IK2,
        inchikey=IK2,
        status=BatchItemStatus.REMOTE_TIMED_OUT,
    )

    result = requeue_timed_out_items([item.pk], actor="tester")

    assert result.items_requeued == 0
    assert result.items_skipped_backpressure == 1
    item.refresh_from_db()
    assert item.status == BatchItemStatus.REMOTE_DELAYED
    assert item.message == "Remote queue is at capacity; retrying later."
    job.refresh_from_db()
    assert job.status == BatchJobStatus.IN_PROGRESS
    event = SystemEvent.objects.filter(event_type="admin_requeue_timed_out").latest(
        "created_at"
    )
    assert event.payload["items_skipped_backpressure"] == 1


def test_requeue_bulk_parks_when_queue_is_already_full(settings, monkeypatch):
    from apps.batches.admin_ops import requeue_timed_out_items

    settings.REMOTE_MAX_QUEUE_DEPTH = 1
    RemoteTask.objects.create(inchikey=IK1, state=TaskState.READY)
    job = BatchJob.objects.create(
        secret_hash="x",
        status=BatchJobStatus.COMPLETE,
        total_count=3,
        invalid_count=3,
    )
    items = [
        BatchItem.objects.create(
            job=job,
            row_order=index,
            original_input=inchikey,
            inchikey=inchikey,
            status=BatchItemStatus.REMOTE_TIMED_OUT,
        )
        for index, inchikey in enumerate((IK2, IK3, "GGGGGGGGGGGGGG-HHHHHHHHHH-N"))
    ]

    def unexpected_enqueue(*_args, **_kwargs):
        raise AssertionError("queue-full requeue must bulk park without enqueueing")

    monkeypatch.setattr(
        "apps.batches.services._get_or_create_remote_task",
        unexpected_enqueue,
    )

    result = requeue_timed_out_items([item.pk for item in items], actor="tester")

    assert result.items_requeued == 0
    assert result.items_skipped_backpressure == 3
    assert list(
        BatchItem.objects.filter(pk__in=[item.pk for item in items])
        .order_by("row_order")
        .values_list("status", "message")
    ) == [
        (
            BatchItemStatus.REMOTE_DELAYED,
            "Remote queue is at capacity; retrying later.",
        ),
        (
            BatchItemStatus.REMOTE_DELAYED,
            "Remote queue is at capacity; retrying later.",
        ),
        (
            BatchItemStatus.REMOTE_DELAYED,
            "Remote queue is at capacity; retrying later.",
        ),
    ]


def test_requeue_locks_only_batch_items(monkeypatch):
    from django.db.models.query import QuerySet

    from apps.batches.admin_ops import requeue_timed_out_items

    job = BatchJob.objects.create(
        secret_hash="x",
        status=BatchJobStatus.COMPLETE,
        total_count=1,
        invalid_count=1,
    )
    item = BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=IK1,
        inchikey=IK1,
        status=BatchItemStatus.REMOTE_TIMED_OUT,
    )
    RemoteTask.objects.create(inchikey=IK1, state=TaskState.READY)
    batch_item_lock_kwargs = []
    original_select_for_update = QuerySet.select_for_update

    def spy_select_for_update(self, *args, **kwargs):
        if self.model is BatchItem:
            batch_item_lock_kwargs.append(kwargs.copy())
        return original_select_for_update(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", spy_select_for_update)

    result = requeue_timed_out_items([item.pk], actor="tester")

    assert result.items_requeued == 1
    assert batch_item_lock_kwargs
    assert batch_item_lock_kwargs[0]["of"] == ("self",)


def test_requeue_stops_new_enqueue_attempts_after_backpressure(monkeypatch):
    from apps.batches.admin_ops import requeue_timed_out_items
    from apps.remote.queue import RemoteQueueBackpressure

    job = BatchJob.objects.create(
        secret_hash="x",
        status=BatchJobStatus.COMPLETE,
        total_count=3,
        invalid_count=3,
    )
    items = [
        BatchItem.objects.create(
            job=job,
            row_order=index,
            original_input=inchikey,
            inchikey=inchikey,
            status=BatchItemStatus.REMOTE_TIMED_OUT,
        )
        for index, inchikey in enumerate((IK1, IK2, IK3))
    ]
    enqueue_attempts: list[str] = []

    def reject_enqueue(inchikey, compound, *, principal=None, priority=0):
        enqueue_attempts.append(inchikey)
        raise RemoteQueueBackpressure("remote queue depth limit reached")

    monkeypatch.setattr(
        "apps.batches.services._get_or_create_remote_task",
        reject_enqueue,
    )

    result = requeue_timed_out_items([item.pk for item in items], actor="tester")

    assert enqueue_attempts == [IK1]
    assert result.items_requeued == 0
    assert result.items_skipped_backpressure == 3
    assert list(
        BatchItem.objects.filter(pk__in=[item.pk for item in items])
        .order_by("row_order")
        .values_list("status", "message")
    ) == [
        (
            BatchItemStatus.REMOTE_DELAYED,
            "Remote queue is at capacity; retrying later.",
        ),
        (
            BatchItemStatus.REMOTE_DELAYED,
            "Remote queue is at capacity; retrying later.",
        ),
        (
            BatchItemStatus.REMOTE_DELAYED,
            "Remote queue is at capacity; retrying later.",
        ),
    ]


def test_batchitem_admin_retry_timed_out_action(admin_client):
    job = BatchJob.objects.create(
        secret_hash="x",
        status=BatchJobStatus.COMPLETE,
        total_count=1,
        invalid_count=1,
    )
    item = BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=IK1,
        inchikey=IK1,
        status=BatchItemStatus.REMOTE_TIMED_OUT,
    )

    response = admin_client.post(
        reverse("admin:batches_batchitem_changelist"),
        {"action": "retry_timed_out", "_selected_action": [str(item.pk)]},
    )

    assert response.status_code == 302
    item.refresh_from_db()
    assert item.status == BatchItemStatus.PENDING_CLASSIFICATION
    assert SystemEvent.objects.filter(event_type="admin_requeue_timed_out").exists()


def test_batchitem_admin_retry_timed_out_action_bulk_parks_when_queue_full(
    admin_client, settings, monkeypatch
):
    settings.REMOTE_MAX_QUEUE_DEPTH = 1
    RemoteTask.objects.create(inchikey=IK1, state=TaskState.READY)
    job = BatchJob.objects.create(
        secret_hash="x",
        status=BatchJobStatus.COMPLETE,
        total_count=2,
        invalid_count=2,
    )
    items = [
        BatchItem.objects.create(
            job=job,
            row_order=index,
            original_input=inchikey,
            inchikey=inchikey,
            status=BatchItemStatus.REMOTE_TIMED_OUT,
        )
        for index, inchikey in enumerate((IK2, IK3))
    ]

    def unexpected_enqueue(*_args, **_kwargs):
        raise AssertionError("queue-full admin action must not enqueue per row")

    monkeypatch.setattr(
        "apps.batches.services._get_or_create_remote_task",
        unexpected_enqueue,
    )

    response = admin_client.post(
        reverse("admin:batches_batchitem_changelist"),
        {
            "action": "retry_timed_out",
            "_selected_action": [str(item.pk) for item in items],
        },
    )

    assert response.status_code == 302
    assert list(
        BatchItem.objects.filter(pk__in=[item.pk for item in items])
        .order_by("row_order")
        .values_list("status", "message")
    ) == [
        (
            BatchItemStatus.REMOTE_DELAYED,
            "Remote queue is at capacity; retrying later.",
        ),
        (
            BatchItemStatus.REMOTE_DELAYED,
            "Remote queue is at capacity; retrying later.",
        ),
    ]
