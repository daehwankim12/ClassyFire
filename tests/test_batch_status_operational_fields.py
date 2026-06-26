"""Tests for Task 9 (F-19): Batch status API exposes operational processing fields.

The status endpoint must return fields that tell a caller what to do next
(wait for ingest worker / wait for remote capacity / submit missing structures)
instead of just an ambiguous top-level ``status`` string.

URL name: the registered name under the ``batches`` app namespace is
``batches:job_status`` (path("jobs/<str:job_id>", ..., name="job_status") in
apps/batches/urls.py, included under app_name="batches").
The brief's placeholder name "batch_status_api" does not exist in the router;
the real namespaced name used here is "batches:job_status".
"""
from __future__ import annotations

import pytest
from django.contrib.auth.hashers import make_password
from django.urls import reverse
from django.utils import timezone

from apps.batches.models import BatchItem, BatchJob
from apps.batches.services import create_job, create_job_records
from apps.core.enums import (
    BatchItemStatus,
    BatchProcessingState,
    CircuitState,
    ProviderControlState,
    TaskState,
)
from apps.remote.models import (
    RemoteAttempt,
    RemoteCircuit,
    RemoteProviderControl,
    RemoteTask,
)

IK_A = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
IK_B = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
GHOST_SMILES = "N[C@@H](Cc1c[nH]c2ccccc12)C(=O)O"


def _delayed_job_with_item(*, inchikey: str = IK_A, message: str | None = None):
    plaintext_secret = "delayed-secret"
    job = BatchJob.objects.create(
        secret_hash=make_password(plaintext_secret),
        processing_state=BatchProcessingState.REMOTE_PENDING,
        remote_pending_count=1,
        total_count=1,
    )
    item = BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=inchikey,
        inchikey=inchikey,
        status=BatchItemStatus.REMOTE_DELAYED,
        message=message,
    )
    return job, plaintext_secret, item


def _breakdown_by_code(payload: dict) -> dict[str, dict]:
    return {entry["code"]: entry for entry in payload["remote_delayed_breakdown"]}


@pytest.mark.django_db(transaction=True)
def test_status_api_exposes_operational_processing_fields(client):
    """New operational fields appear when there is a PENDING_NORMALIZATION item."""
    # Create a job via the service layer so we get a real bcrypt secret_hash
    # and can authenticate via X-Job-Secret.
    rows = [("CCO", None)]  # minimal row: (normalized_input, external_id)
    from apps.batches.ingest import parse_text_input

    rows = list(parse_text_input("CCO"))
    job, plaintext_secret = create_job_records(rows, remote_enabled=False)

    # Force the item into PENDING_NORMALIZATION and the job into LOCAL_LOOKUP
    # so the test exercises the exact next_action branch the brief specifies.
    BatchItem.objects.filter(job=job).update(status=BatchItemStatus.PENDING_NORMALIZATION)
    BatchJob.objects.filter(pk=job.pk).update(
        processing_state=BatchProcessingState.LOCAL_LOOKUP
    )
    job.refresh_from_db()

    url = reverse("batches:job_status", kwargs={"job_id": job.pk})
    response = client.get(url, HTTP_X_JOB_SECRET=plaintext_secret)
    assert response.status_code == 200
    payload = response.json()
    assert payload["processing_state"] == BatchProcessingState.LOCAL_LOOKUP
    assert payload["normalization_pending_count"] == 1
    assert payload["remote_delayed_count"] == 0
    assert payload["next_action"] == "wait_for_ingest_worker"


@pytest.mark.django_db(transaction=True)
def test_status_api_remote_delayed_next_action(client):
    """Generic REMOTE_DELAYED rows surface an explicit remote-recovery action."""
    from apps.batches.ingest import parse_text_input

    rows = list(parse_text_input("CCO"))
    job, plaintext_secret = create_job_records(rows, remote_enabled=False)

    BatchItem.objects.filter(job=job).update(status=BatchItemStatus.REMOTE_DELAYED)

    url = reverse("batches:job_status", kwargs={"job_id": job.pk})
    response = client.get(url, HTTP_X_JOB_SECRET=plaintext_secret)
    assert response.status_code == 200
    payload = response.json()
    assert payload["remote_delayed_count"] == 1
    assert payload["normalization_pending_count"] == 0
    assert payload["next_action"] == "wait_for_remote_recovery"


@pytest.mark.django_db(transaction=True)
def test_status_api_remote_delayed_breakdown_distinguishes_wishart_outage(client):
    job, plaintext_secret, item = _delayed_job_with_item(inchikey=IK_A)
    task = RemoteTask.objects.create(
        inchikey=IK_A,
        state=TaskState.RETRY_WAIT,
        next_run_at=timezone.now(),
    )
    RemoteAttempt.objects.create(
        provider="wishart",
        operation=RemoteAttempt.OPERATION_ENTITY,
        task=task,
        request_inchikey=IK_A,
        classification="RETRYABLE",
        http_status=503,
        detail="HTTP 503",
    )

    url = reverse("batches:job_status", kwargs={"job_id": job.pk})
    response = client.get(url, HTTP_X_JOB_SECRET=plaintext_secret)

    assert response.status_code == 200
    payload = response.json()
    breakdown = _breakdown_by_code(payload)
    assert breakdown["wishart_retry"]["count"] == 1
    assert breakdown["wishart_retry"]["label"] == "Wishart outage / retrying"
    assert item.inchikey in breakdown["wishart_retry"]["inchikeys"]
    assert payload["next_action"] == "wait_for_wishart_recovery"


@pytest.mark.django_db(transaction=True)
def test_status_api_remote_delayed_breakdown_distinguishes_circuit_and_auth(client):
    job, plaintext_secret, _item = _delayed_job_with_item(inchikey=IK_A)
    BatchItem.objects.create(
        job=job,
        row_order=1,
        original_input=IK_B,
        inchikey=IK_B,
        status=BatchItemStatus.REMOTE_DELAYED,
    )
    RemoteTask.objects.create(
        inchikey=IK_A,
        state=TaskState.DEFERRED,
        next_run_at=timezone.now(),
    )
    RemoteTask.objects.create(
        inchikey=IK_B,
        state=TaskState.DEFERRED,
        next_run_at=timezone.now(),
    )
    RemoteCircuit.objects.create(
        provider="wishart",
        state=CircuitState.OPEN,
        consecutive_failures=5,
        opened_at=timezone.now(),
    )
    RemoteProviderControl.objects.create(
        provider="wishart",
        state=ProviderControlState.AUTH_BLOCKED,
        blocked_reason="HTTP 401",
    )

    url = reverse("batches:job_status", kwargs={"job_id": job.pk})
    response = client.get(url, HTTP_X_JOB_SECRET=plaintext_secret)

    assert response.status_code == 200
    payload = response.json()
    breakdown = _breakdown_by_code(payload)
    assert breakdown["wishart_auth_blocked"]["count"] == 2
    assert breakdown["wishart_auth_blocked"]["label"] == "Wishart auth blocked"
    assert "circuit_open" not in breakdown
    assert payload["next_action"] == "fix_wishart_auth"


@pytest.mark.django_db(transaction=True)
def test_status_api_remote_delayed_breakdown_uses_item_messages(client):
    job, plaintext_secret, _item = _delayed_job_with_item(
        inchikey=IK_A,
        message="Remote queue is at capacity; retrying later.",
    )
    BatchItem.objects.create(
        job=job,
        row_order=1,
        original_input=IK_B,
        inchikey=IK_B,
        status=BatchItemStatus.REMOTE_DELAYED,
        message="Remote budget exceeded for this principal.",
    )

    url = reverse("batches:job_status", kwargs={"job_id": job.pk})
    response = client.get(url, HTTP_X_JOB_SECRET=plaintext_secret)

    assert response.status_code == 200
    payload = response.json()
    breakdown = _breakdown_by_code(payload)
    assert breakdown["queue_capacity"]["count"] == 1
    assert breakdown["budget_exceeded"]["count"] == 1
    assert payload["next_action"] == "wait_for_remote_capacity"


@pytest.mark.django_db(transaction=True)
def test_status_api_remote_disabled_breakdown_for_structured_miss(client, monkeypatch):
    from apps.batches.ingest import parse_text_input

    monkeypatch.setattr(
        "apps.batches.services.lookup_current_classifications",
        lambda _inchikeys: {},
    )
    rows = list(parse_text_input(GHOST_SMILES))
    job, plaintext_secret = create_job(rows, remote_enabled=False)

    url = reverse("batches:job_status", kwargs={"job_id": job.pk})
    response = client.get(url, HTTP_X_JOB_SECRET=plaintext_secret)

    assert response.status_code == 200
    payload = response.json()
    breakdown = _breakdown_by_code(payload)
    assert breakdown["remote_disabled"]["count"] == 1
    assert payload["next_action"] == "enable_remote_or_resubmit"


@pytest.mark.django_db(transaction=True)
def test_status_api_need_structure_next_action(client):
    """next_action == "submit_missing_structures" when only NEED_STRUCTURE items remain."""
    from apps.batches.ingest import parse_text_input

    rows = list(parse_text_input("CCO"))
    job, plaintext_secret = create_job_records(rows, remote_enabled=False)

    BatchItem.objects.filter(job=job).update(status=BatchItemStatus.NEED_STRUCTURE)

    url = reverse("batches:job_status", kwargs={"job_id": job.pk})
    response = client.get(url, HTTP_X_JOB_SECRET=plaintext_secret)
    assert response.status_code == 200
    payload = response.json()
    assert payload["need_structure_count"] == 1
    assert payload["normalization_pending_count"] == 0
    assert payload["remote_delayed_count"] == 0
    assert payload["next_action"] == "submit_missing_structures"


@pytest.mark.django_db(transaction=True)
def test_status_api_no_action_needed(client):
    """next_action == "none" when no operational-state items remain."""
    from apps.batches.ingest import parse_text_input

    rows = list(parse_text_input("CCO"))
    job, plaintext_secret = create_job_records(rows, remote_enabled=False)

    # Leave items in whatever state create_job_records put them in — they will
    # not be in any of the three action-state categories.
    BatchItem.objects.filter(job=job).update(status=BatchItemStatus.INVALID_INPUT)
    # Move the job to a terminal processing_state so it is no longer claimable;
    # only then is "none" the correct next_action.
    BatchJob.objects.filter(pk=job.pk).update(
        processing_state=BatchProcessingState.COMPLETE
    )

    url = reverse("batches:job_status", kwargs={"job_id": job.pk})
    response = client.get(url, HTTP_X_JOB_SECRET=plaintext_secret)
    assert response.status_code == 200
    payload = response.json()
    assert payload["normalization_pending_count"] == 0
    assert payload["remote_delayed_count"] == 0
    assert payload["need_structure_count"] == 0
    assert payload["next_action"] == "none"


@pytest.mark.django_db(transaction=True)
def test_status_api_uploading_job_without_items_waits_for_ingest_worker(client):
    """Fresh deferred uploads have no items yet but still need worker ingest."""
    plaintext_secret = "upload-secret"
    job = BatchJob.objects.create(
        secret_hash=make_password(plaintext_secret),
        processing_state=BatchProcessingState.UPLOADING,
        upload_artifact_path="uploads/pending.csv",
    )

    url = reverse("batches:job_status", kwargs={"job_id": job.pk})
    response = client.get(url, HTTP_X_JOB_SECRET=plaintext_secret)
    assert response.status_code == 200
    payload = response.json()
    assert payload["processing_state"] == BatchProcessingState.UPLOADING
    assert payload["normalization_pending_count"] == 0
    assert payload["remote_delayed_count"] == 0
    assert payload["need_structure_count"] == 0
    assert payload["next_action"] == "wait_for_ingest_worker"


@pytest.mark.django_db(transaction=True)
def test_status_api_remote_pending_job_without_items_waits_for_remote_capacity(client):
    """A claimable REMOTE_PENDING job with no live items waits on remote capacity."""
    plaintext_secret = "remote-secret"
    job = BatchJob.objects.create(
        secret_hash=make_password(plaintext_secret),
        processing_state=BatchProcessingState.REMOTE_PENDING,
    )

    url = reverse("batches:job_status", kwargs={"job_id": job.pk})
    response = client.get(url, HTTP_X_JOB_SECRET=plaintext_secret)
    assert response.status_code == 200
    payload = response.json()
    assert payload["processing_state"] == BatchProcessingState.REMOTE_PENDING
    assert payload["normalization_pending_count"] == 0
    assert payload["remote_delayed_count"] == 0
    assert payload["need_structure_count"] == 0
    assert payload["next_action"] == "wait_for_remote_capacity"


@pytest.mark.django_db(transaction=True)
def test_status_api_old_keys_preserved(client):
    """The five new operational fields must not remove existing response keys."""
    from apps.batches.ingest import parse_text_input

    rows = list(parse_text_input("CCO"))
    job, plaintext_secret = create_job_records(rows, remote_enabled=False)

    url = reverse("batches:job_status", kwargs={"job_id": job.pk})
    response = client.get(url, HTTP_X_JOB_SECRET=plaintext_secret)
    assert response.status_code == 200
    payload = response.json()

    # Pre-existing keys that other callers depend on — must all still be present.
    for key in (
        "id",
        "status",
        "total_count",
        "completed_count",
        "local_hit_count",
        "seed_hit_count",
        "remote_pending_count",
        "need_structure_count",
        "invalid_count",
        "remote_enabled",
        "created_at",
        "completed_at",
    ):
        assert key in payload, f"Pre-existing key {key!r} missing from status response"

    # New keys must also be present.
    for key in (
        "processing_state",
        "normalization_pending_count",
        "remote_delayed_count",
        "remote_delayed_breakdown",
        "next_action",
    ):
        assert key in payload, f"New operational key {key!r} missing from status response"
