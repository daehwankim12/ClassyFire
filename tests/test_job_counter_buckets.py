"""Regression: progress-bar counter buckets must be disjoint.

Reported bug — invalidated ("무효처리된") items showed up in the "완료"(completed)
bucket of the web progress bar. Root cause: ``recompute_job_counters`` counted
``INVALID_INPUT`` in BOTH the terminal-complete set (→ ``completed_count``) AND
the ``invalid`` aggregate, double-counting it; the failure states
``RESULT_KEY_MISMATCH`` / ``REMOTE_TIMED_OUT`` were likewise counted as
"completed" even though exports render them as "Failed".

``completed_count`` must mean *successfully classified only*; every terminal
failure belongs in ``invalid_count``. The four progress segments
(completed / need_structure / invalid / pending) must sum to the total with no
item counted twice.
"""
from __future__ import annotations

import pytest

from apps.batches.models import BatchItem, BatchJob
from apps.batches.services import recompute_job_counters
from apps.core.enums import BatchItemStatus


@pytest.mark.django_db
def test_failures_are_counted_invalid_not_completed():
    job = BatchJob.objects.create(secret_hash="x")
    statuses = [
        BatchItemStatus.FOUND_LIVE_CACHE,        # classified → 완료
        BatchItemStatus.FOUND_WISHART_ENTITY,    # classified → 완료
        BatchItemStatus.COMPLETE_AFTER_WISHART,  # classified → 완료
        BatchItemStatus.INVALID_INPUT,           # failure   → 무효
        BatchItemStatus.RESULT_KEY_MISMATCH,     # failure   → 무효
        BatchItemStatus.REMOTE_TIMED_OUT,        # failure   → 무효
        BatchItemStatus.NEED_STRUCTURE,          # awaits user
    ]
    for row, status in enumerate(statuses):
        BatchItem.objects.create(
            job=job, row_order=row, original_input=f"in-{row}", status=status
        )

    recompute_job_counters(job.pk)
    job.refresh_from_db()

    assert job.total_count == 7
    assert job.completed_count == 3        # only the successfully classified rows
    assert job.invalid_count == 3          # all terminal failures, not just INVALID_INPUT
    assert job.need_structure_count == 1
    # Disjoint buckets: nothing double-counted, segments sum to the total.
    assert (
        job.completed_count + job.invalid_count + job.need_structure_count
        == job.total_count
    )


@pytest.mark.django_db
def test_invalid_filter_matches_invalid_counter_bucket(client):
    """Web and API INVALID filters include every status counted as invalid."""
    from django.contrib.auth.hashers import make_password

    from apps.batches.api import STATUS_FILTER_MAP as API_STATUS_FILTER_MAP
    from apps.batches.views import STATUS_FILTER_MAP as WEB_STATUS_FILTER_MAP
    from apps.batches.views import _filter_items

    secret = "filter-secret"
    job = BatchJob.objects.create(secret_hash=make_password(secret))
    expected_invalid_statuses = [
        BatchItemStatus.INVALID_INPUT,
        BatchItemStatus.RESULT_KEY_MISMATCH,
        BatchItemStatus.REMOTE_TIMED_OUT,
    ]
    for row, status in enumerate(
        [BatchItemStatus.FOUND_LIVE_CACHE, *expected_invalid_statuses]
    ):
        BatchItem.objects.create(
            job=job,
            row_order=row,
            original_input=f"filter-{row}",
            status=status,
        )

    recompute_job_counters(job.pk)
    job.refresh_from_db()

    assert job.invalid_count == len(expected_invalid_statuses)
    assert WEB_STATUS_FILTER_MAP["INVALID"] == expected_invalid_statuses
    assert API_STATUS_FILTER_MAP["INVALID"] == expected_invalid_statuses
    assert list(_filter_items(job, "INVALID", "").values_list("status", flat=True)) == [
        status.value for status in expected_invalid_statuses
    ]

    response = client.get(
        f"/api/jobs/{job.pk}/items?status_filter=INVALID",
        HTTP_X_JOB_SECRET=secret,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == len(expected_invalid_statuses)
    assert [item["status"] for item in data["items"]] == [
        status.value for status in expected_invalid_statuses
    ]
