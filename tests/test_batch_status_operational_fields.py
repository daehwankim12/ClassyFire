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

from apps.batches.models import BatchItem, BatchJob
from apps.batches.services import create_job_records
from apps.core.enums import BatchItemStatus, BatchProcessingState


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
    """next_action == "wait_for_remote_capacity" when there are REMOTE_DELAYED items."""
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
    assert payload["next_action"] == "wait_for_remote_capacity"


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
        "next_action",
    ):
        assert key in payload, f"New operational key {key!r} missing from status response"
