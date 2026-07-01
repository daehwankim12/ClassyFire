from __future__ import annotations

import pytest
from django.test import override_settings

IK_A = "RDHQFKQIGNGIED-MRVPVSSYSA-N"
IK_B = "WQZGKKKJIJFFOK-GASJEMHNSA-N"
IK_C = "HXKKHQJGJAFBHI-UHFFFAOYSA-N"
GHOST_SMILES = "CCO"


def _fake_inchikey(index: int) -> str:
    letters = []
    value = index
    for _ in range(3):
        letters.append(chr(ord("A") + (value % 26)))
        value //= 26
    return f"{''.join(letters)}AAAAAAAAAAA-BBBBBBBBBB-C"


@pytest.mark.django_db(transaction=True)
@override_settings(REMOTE_DAILY_BUDGET_PER_PRINCIPAL=0)
def test_remote_budget_exhaustion_delays_item_without_task():
    from apps.batches.ingest import parse_text_input
    from apps.batches.models import BatchItem
    from apps.batches.services import create_job
    from apps.core.enums import BatchItemStatus
    from apps.remote.models import RemoteBudgetLedger, RemoteTask

    job, _secret = create_job(
        parse_text_input(GHOST_SMILES),
        remote_enabled=True,
        remote_principal="principal-a",
    )
    item = BatchItem.objects.get(job=job)

    assert item.status == BatchItemStatus.REMOTE_DELAYED
    assert item.message == "Remote budget exceeded for this principal."
    assert RemoteTask.objects.count() == 0
    assert RemoteBudgetLedger.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_equal_priority_claims_rotate_between_principals():
    from apps.core.enums import TaskState
    from apps.remote import queue
    from apps.remote.models import RemoteTask

    first_a = RemoteTask.objects.create(
        inchikey=IK_A,
        principal="principal-a",
        state=TaskState.READY,
        priority=0,
    )
    RemoteTask.objects.create(
        inchikey=IK_B,
        principal="principal-a",
        state=TaskState.READY,
        priority=0,
    )
    first_b = RemoteTask.objects.create(
        inchikey=IK_C,
        principal="principal-b",
        state=TaskState.READY,
        priority=0,
    )

    claimed_1 = queue.claim_task("worker-1")
    claimed_2 = queue.claim_task("worker-2")

    assert claimed_1 is not None
    assert claimed_1.pk == first_a.pk
    assert claimed_2 is not None
    assert claimed_2.pk == first_b.pk


@pytest.mark.django_db(transaction=True)
def test_enqueue_existing_task_raises_priority_without_lowering():
    from apps.remote import queue

    task = queue.enqueue_task(IK_A, priority=10)
    assert task is not None
    assert task.priority == 10

    again = queue.enqueue_task(IK_A, priority=100)
    assert again is not None
    again.refresh_from_db()
    assert again.pk == task.pk
    assert again.priority == 100

    lower = queue.enqueue_task(IK_A, priority=1)
    assert lower is not None
    lower.refresh_from_db()
    assert lower.pk == task.pk
    assert lower.priority == 100


def test_budget_advisory_lock_keys_are_stable_signed_ints():
    from datetime import UTC, datetime

    from apps.remote.budget import budget_advisory_lock_keys

    window = datetime(2026, 6, 24, tzinfo=UTC)
    first = budget_advisory_lock_keys("principal-a", window)
    again = budget_advisory_lock_keys("principal-a", window)
    other = budget_advisory_lock_keys("principal-b", window)

    assert first == again
    assert first != other
    assert all(-(2**31) <= value < 2**31 for value in first)


@pytest.mark.django_db(transaction=True)
def test_fair_claim_does_not_hide_principal_behind_first_50_tasks():
    from apps.core.enums import TaskState
    from apps.remote import queue
    from apps.remote.models import RemoteTask

    first_a = RemoteTask.objects.create(
        inchikey=_fake_inchikey(0),
        principal="principal-a",
        state=TaskState.READY,
        priority=0,
    )
    for index in range(1, 51):
        RemoteTask.objects.create(
            inchikey=_fake_inchikey(index),
            principal="principal-a",
            state=TaskState.READY,
            priority=0,
        )
    first_b = RemoteTask.objects.create(
        inchikey=_fake_inchikey(100),
        principal="principal-b",
        state=TaskState.READY,
        priority=0,
    )

    claimed_1 = queue.claim_task("worker-1")
    claimed_2 = queue.claim_task("worker-2")

    assert claimed_1 is not None
    assert claimed_1.pk == first_a.pk
    assert claimed_2 is not None
    assert claimed_2.pk == first_b.pk


@pytest.mark.django_db(transaction=True)
def test_fair_claim_locks_principal_state_rows_before_task_update():
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.core.enums import TaskState
    from apps.remote import queue
    from apps.remote.models import RemoteTask

    RemoteTask.objects.create(
        inchikey=IK_A,
        principal="principal-a",
        state=TaskState.READY,
    )
    RemoteTask.objects.create(
        inchikey=IK_B,
        principal="principal-b",
        state=TaskState.READY,
    )

    with CaptureQueriesContext(connection) as captured:
        claimed = queue.claim_task("worker-1")

    assert claimed is not None
    queries = [query["sql"] for query in captured.captured_queries]
    state_lock_index = next(
        index
        for index, sql in enumerate(queries)
        if '"remote_principal_states"' in sql and "FOR UPDATE" in sql
    )
    task_update_index = next(
        index
        for index, sql in enumerate(queries)
        if sql.startswith('UPDATE "remote_tasks"')
    )
    assert state_lock_index < task_update_index


@pytest.mark.django_db(transaction=True)
def test_claim_retries_when_locked_frontier_disappears(monkeypatch):
    from apps.core.enums import TaskState
    from apps.remote import queue
    from apps.remote.models import RemoteTask

    high = RemoteTask.objects.create(
        inchikey=IK_A,
        principal="principal-a",
        state=TaskState.READY,
        priority=10,
    )
    low = RemoteTask.objects.create(
        inchikey=IK_B,
        principal="principal-b",
        state=TaskState.READY,
        priority=0,
    )
    original = queue._claim_task_once
    calls = 0

    def racing_claim_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            RemoteTask.objects.filter(pk=high.pk).update(state=TaskState.LOCKED)
            return None, True
        return original(*args, **kwargs)

    monkeypatch.setattr(queue, "_claim_task_once", racing_claim_once)

    claimed = queue.claim_task("worker-1")

    assert claimed is not None
    assert claimed.pk == low.pk
    assert calls == 2
