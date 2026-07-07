"""Budget-delayed REMOTE_DELAYED rows must retry once budget is available.

Queue backpressure and budget denial both happen before a RemoteTask exists,
so the BatchItem row is the durable retry record for both. These tests pin the
sweep contract: budget rows drain automatically when the daily window resets
(or an operator raises the budget), the sweep stops probing after the first
denial, and operator/action-gated rows are never touched.
"""
from __future__ import annotations

import pytest

from apps.core.enums import (
    BatchItemStatus,
    BatchJobStatus,
    BatchProcessingState,
    TaskState,
)

BUDGET_MESSAGE = "Remote budget exceeded for this principal."

KEYS = [
    "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
    "QTBSBXVTEAMEQO-UHFFFAOYSA-N",
    "XLYOFNOQVPJJNP-UHFFFAOYSA-N",
]


def _delayed_job(item_messages: list[str]):
    from apps.batches.models import BatchItem, BatchJob

    job = BatchJob.objects.create(
        secret_hash="x",
        status=BatchJobStatus.IN_PROGRESS,
        processing_state=BatchProcessingState.REMOTE_PENDING,
        remote_enabled=True,
        total_count=len(item_messages),
        remote_pending_count=len(item_messages),
    )
    items = [
        BatchItem.objects.create(
            job=job,
            row_order=index,
            original_input=key,
            inchikey=key,
            status=BatchItemStatus.REMOTE_DELAYED,
            message=message,
        )
        for index, (key, message) in enumerate(zip(KEYS, item_messages, strict=False))
    ]
    return job, items


def _run_sweep(job):
    from apps.batches.services import (
        _retry_transiently_delayed_remote_items,
        claim_from_job,
    )

    _retry_transiently_delayed_remote_items(
        claim_from_job(job),
        remote_enabled=True,
        remote_principal=job.remote_principal,
        job_priority=job.priority,
    )


@pytest.mark.django_db(transaction=True)
def test_budget_delayed_item_is_retried_by_ingest_worker_once_budget_refills(
    settings,
):
    from apps.batches.ingest import parse_text_input
    from apps.batches.models import BatchItem
    from apps.batches.services import create_job, run_pending_jobs
    from apps.remote.models import RemoteTask

    settings.REMOTE_DAILY_BUDGET_PER_PRINCIPAL = 0
    job, _secret = create_job(
        parse_text_input("C1CCCCC1"),
        remote_enabled=True,
        remote_principal="principal-a",
    )
    item = BatchItem.objects.get(job=job)
    assert item.status == BatchItemStatus.REMOTE_DELAYED
    assert item.message == BUDGET_MESSAGE
    assert job.processing_state == BatchProcessingState.REMOTE_PENDING
    assert RemoteTask.objects.count() == 0

    settings.REMOTE_DAILY_BUDGET_PER_PRINCIPAL = 10

    assert run_pending_jobs("worker-1", max_jobs=1) == 1
    item.refresh_from_db()
    assert item.status == BatchItemStatus.PENDING_CLASSIFICATION
    assert item.message is None
    assert (
        RemoteTask.objects.filter(
            inchikey=item.inchikey, state=TaskState.READY
        ).count()
        == 1
    )


@pytest.mark.django_db(transaction=True)
def test_sweep_stops_probing_after_first_budget_denial(monkeypatch):
    from apps.remote.budget import RemoteBudgetExceededError

    job, items = _delayed_job([BUDGET_MESSAGE] * 3)
    attempts: list[str] = []

    monkeypatch.setattr(
        "apps.batches.services.lookup_current_classification",
        lambda _inchikey: None,
    )

    def deny(inchikey, _compound, *, principal=None, priority=0):
        attempts.append(inchikey)
        raise RemoteBudgetExceededError(principal)

    monkeypatch.setattr(
        "apps.batches.services._get_or_create_remote_task", deny
    )

    _run_sweep(job)

    # All rows in one job share one principal: after the first denial no other
    # row can succeed this pass, so exactly one probe is spent.
    assert attempts == [items[0].inchikey]
    for item in items:
        item.refresh_from_db()
        assert item.status == BatchItemStatus.REMOTE_DELAYED
        assert item.message == BUDGET_MESSAGE


@pytest.mark.django_db(transaction=True)
def test_partial_budget_drains_what_it_can_and_parks_the_rest(settings):
    from apps.remote.models import RemoteTask

    settings.REMOTE_DAILY_BUDGET_PER_PRINCIPAL = 2
    job, items = _delayed_job([BUDGET_MESSAGE] * 3)

    _run_sweep(job)

    for item in items:
        item.refresh_from_db()
    assert [item.status for item in items] == [
        BatchItemStatus.PENDING_CLASSIFICATION,
        BatchItemStatus.PENDING_CLASSIFICATION,
        BatchItemStatus.REMOTE_DELAYED,
    ]
    assert items[2].message == BUDGET_MESSAGE
    assert RemoteTask.objects.filter(state=TaskState.READY).count() == 2


@pytest.mark.django_db(transaction=True)
def test_action_gated_delayed_rows_are_not_retried(settings, monkeypatch):
    from apps.batches.remote_delay import (
        REMOTE_DELAYED_DEFAULT_MESSAGE,
        REMOTE_DISABLED_MESSAGE,
    )

    settings.REMOTE_DAILY_BUDGET_PER_PRINCIPAL = 10
    messages = [REMOTE_DISABLED_MESSAGE, REMOTE_DELAYED_DEFAULT_MESSAGE]
    job, items = _delayed_job(messages)
    attempts: list[str] = []

    monkeypatch.setattr(
        "apps.batches.services._get_or_create_remote_task",
        lambda inchikey, _compound, **kwargs: attempts.append(inchikey),
    )

    _run_sweep(job)

    assert attempts == []
    for item, message in zip(items, messages, strict=True):
        item.refresh_from_db()
        assert item.status == BatchItemStatus.REMOTE_DELAYED
        assert item.message == message
