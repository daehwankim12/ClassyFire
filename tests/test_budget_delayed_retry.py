"""Budget-delayed REMOTE_DELAYED rows must retry once budget is available.

Queue backpressure and budget denial both happen before a RemoteTask exists,
so the BatchItem row is the durable retry record for both. These tests pin the
sweep contract: budget rows drain automatically when the daily window resets
(or an operator raises the budget), the sweep stops probing after the first
denial, and operator/action-gated rows are never touched.
"""
from __future__ import annotations

from datetime import timedelta

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

    return _retry_transiently_delayed_remote_items(
        claim_from_job(job),
        remote_enabled=True,
        remote_principal=job.remote_principal,
        job_priority=job.priority,
    )


def _open_retry_gate(job):
    """Simulate the parked job's retry_not_before gate having elapsed.

    A budget park gates on the next daily budget window (local midnight), so a
    test that wants the worker to re-probe on THIS pass opens the gate rather
    than waiting real hours.
    """
    from django.utils import timezone

    from apps.batches.models import BatchJob

    BatchJob.objects.filter(pk=job.pk).update(retry_not_before=timezone.now())


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
    # The budget park gated retry on the next daily window; simulate that window
    # arriving so the worker re-probes on this pass.
    _open_retry_gate(job)

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
def test_worker_claimed_job_releases_its_claim_and_retries_on_the_next_pass(settings):
    """The PRODUCTION path: the job that hit the budget was CLAIMED by a worker.

    A worker that parked its job and kept the claim would hide it from every
    worker until the crash-detection lease went stale (15 min by default), so
    the budget refill would not drain the rows on the next pass. The parked job
    must be released and gated on retry_not_before instead.
    """
    from django.utils import timezone

    from apps.batches.ingest import parse_text_input
    from apps.batches.models import BatchItem
    from apps.batches.services import create_job_records, run_pending_jobs
    from apps.remote.models import RemoteTask

    settings.REMOTE_DAILY_BUDGET_PER_PRINCIPAL = 0
    job, _secret = create_job_records(
        parse_text_input("C1CCCCC1"),
        remote_enabled=True,
        remote_principal="principal-a",
    )

    assert run_pending_jobs("worker-1", max_jobs=1) == 1
    item = BatchItem.objects.get(job=job)
    assert item.status == BatchItemStatus.REMOTE_DELAYED
    assert item.message == BUDGET_MESSAGE

    job.refresh_from_db()
    assert job.processing_state == BatchProcessingState.REMOTE_PENDING
    # Claim released and gated in the future, not held until the lease goes stale.
    assert job.claimed_by is None
    assert job.retry_not_before is not None
    assert job.retry_not_before > timezone.now()

    settings.REMOTE_DAILY_BUDGET_PER_PRINCIPAL = 10
    _open_retry_gate(job)

    assert run_pending_jobs("worker-2", max_jobs=1) == 1
    item.refresh_from_db()
    assert item.status == BatchItemStatus.PENDING_CLASSIFICATION
    assert item.message is None
    assert (
        RemoteTask.objects.filter(
            inchikey=item.inchikey, state=TaskState.READY
        ).count()
        == 1
    )

    job.refresh_from_db()
    assert job.retry_not_before is None


@pytest.mark.django_db(transaction=True)
def test_parked_job_is_not_reclaimed_before_its_retry_gate_elapses(settings):
    """The retry gate paces re-probing; without it the worker loop would spin.

    ``run_pending_jobs`` claims until nothing is claimable, so a released job
    with no gate would be re-claimed on the very next iteration and burn a
    budget probe per loop.
    """
    from django.utils import timezone

    from apps.batches.ingest import parse_text_input
    from apps.batches.models import BatchItem
    from apps.batches.services import create_job_records, run_pending_jobs

    settings.REMOTE_DAILY_BUDGET_PER_PRINCIPAL = 0
    job, _secret = create_job_records(
        parse_text_input("C1CCCCC1"),
        remote_enabled=True,
        remote_principal="principal-a",
    )
    assert run_pending_jobs("worker-1", max_jobs=1) == 1

    job.refresh_from_db()
    assert job.claimed_by is None
    assert job.retry_not_before is not None
    assert job.retry_not_before > timezone.now()

    settings.REMOTE_DAILY_BUDGET_PER_PRINCIPAL = 10
    item = BatchItem.objects.get(job=job)

    # Gate still closed: the refilled budget does not tempt a worker into the job.
    assert run_pending_jobs("worker-2") == 0
    item.refresh_from_db()
    assert item.status == BatchItemStatus.REMOTE_DELAYED

    _open_retry_gate(job)

    assert run_pending_jobs("worker-2", max_jobs=1) == 1
    item.refresh_from_db()
    assert item.status == BatchItemStatus.PENDING_CLASSIFICATION


def test_budget_park_gates_retry_on_next_budget_window():
    """A spent daily budget only refills at the next window, so gate there —
    not on the short backpressure interval that would probe every minute."""
    from apps.batches.services import _PARK_CAUSE_BUDGET, _delayed_retry_gate
    from apps.remote.budget import next_daily_budget_window_start

    assert _delayed_retry_gate(_PARK_CAUSE_BUDGET) == next_daily_budget_window_start()


def test_backpressure_park_uses_floored_short_interval(settings):
    """Queue capacity frees continuously → short interval; a zero/negative
    REMOTE_DELAY_RETRY_SECONDS must still floor to >= 1s, never gate on 'now'."""
    from django.utils import timezone

    from apps.batches.services import _PARK_CAUSE_BACKPRESSURE, _delayed_retry_gate

    settings.REMOTE_DELAY_RETRY_SECONDS = 0
    before = timezone.now()
    gate = _delayed_retry_gate(_PARK_CAUSE_BACKPRESSURE)
    assert gate >= before + timedelta(seconds=1)
    assert gate < before + timedelta(seconds=5)

    settings.REMOTE_DELAY_RETRY_SECONDS = 45
    before = timezone.now()
    gate = _delayed_retry_gate(_PARK_CAUSE_BACKPRESSURE)
    assert before + timedelta(seconds=44) < gate < before + timedelta(seconds=46)


@pytest.mark.django_db(transaction=True)
def test_sweep_relabels_budget_rows_to_queue_capacity_on_backpressure(monkeypatch):
    """A row that arrived carrying the budget message but now hits BACKPRESSURE
    must be re-labeled: the gate uses the short backpressure interval, so the
    delay-cause message has to agree or the UI shows the wrong cause."""
    from apps.batches.remote_delay import QUEUE_CAPACITY_MESSAGE
    from apps.batches.services import _PARK_CAUSE_BACKPRESSURE
    from apps.remote.queue import RemoteQueueBackpressure

    job, items = _delayed_job([BUDGET_MESSAGE] * 2)

    monkeypatch.setattr(
        "apps.batches.services.lookup_current_classification",
        lambda _inchikey: None,
    )

    def backpressure(inchikey, _compound, *, principal=None, priority=0):
        raise RemoteQueueBackpressure("remote queue depth limit reached")

    monkeypatch.setattr(
        "apps.batches.services._get_or_create_remote_task", backpressure
    )

    cause = _run_sweep(job)

    assert cause == _PARK_CAUSE_BACKPRESSURE
    # First backpressure re-labels the probed row AND the rest of the chunk
    # (capacity is global), so neither keeps the stale budget message.
    for item in items:
        item.refresh_from_db()
        assert item.status == BatchItemStatus.REMOTE_DELAYED
        assert item.message == QUEUE_CAPACITY_MESSAGE
    assert BUDGET_MESSAGE not in {item.message for item in items}


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
