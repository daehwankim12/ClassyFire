"""Tests for the remote queue, worker, retry/backoff, circuit, and fan-out.

All tests run against the REAL Postgres DB using the deterministic MockProvider
(no network). They exercise the FOR UPDATE SKIP LOCKED claim, lock TTL recovery,
per-InChIKey dedupe, entity-first flow, /queries submit + poll, result-key
mismatch guard, fan-out to all waiting items, Retry-After precedence, backoff
schedule, circuit-breaker behaviour, and REMOTE_ENABLED=false.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.batches.models import BatchItem, BatchJob
from apps.classifications.models import ClassificationHistory, ClassyfireLive
from apps.core.enums import (
    BatchItemStatus,
    BatchJobStatus,
    QueryState,
    SourceType,
    TaskState,
)
from apps.remote import queue
from apps.remote.circuit import CircuitBreaker, CircuitState
from apps.remote.mock import MockProvider, build_raw
from apps.remote.models import RemoteQuery, RemoteTask
from apps.remote.rate_limit import RateLimiter
from apps.remote.worker import RemoteWorker

pytestmark = pytest.mark.django_db(transaction=True)

IK_A = "RDHQFKQIGNGIED-MRVPVSSYSA-N"
IK_B = "WQZGKKKJIJFFOK-GASJEMHNSA-N"
IK_C = "AAAABBBBCCCCDD-EEEEFFFFFF-N"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _compound(inchikey: str, smiles: str | None = "CCO"):
    from apps.compounds.models import CompoundStructure

    return CompoundStructure.objects.create(
        original_input=smiles or inchikey,
        canonical_smiles=smiles,
        inchikey=inchikey,
    )


def _job() -> BatchJob:
    return BatchJob.objects.create(secret_hash="x", total_count=0)


def _item(job: BatchJob, inchikey: str, row: int, *, compound=None,
          status=BatchItemStatus.REMOTE_DELAYED) -> BatchItem:
    return BatchItem.objects.create(
        job=job,
        row_order=row,
        original_input=inchikey,
        inchikey=inchikey,
        compound=compound,
        status=status,
    )


def _worker(provider, **kw) -> RemoteWorker:
    return RemoteWorker(
        provider,
        worker_id=kw.pop("worker_id", "w-test"),
        rate_limiter=kw.pop("rate_limiter", RateLimiter(1000, 1000)),
        circuit=kw.pop("circuit", CircuitBreaker(failure_threshold=1000)),
        **kw,
    )


# ---------------------------------------------------------------------------
# claim protocol + dedupe
# ---------------------------------------------------------------------------


def test_claim_locks_task_and_skips_locked():
    queue.enqueue_task(IK_A)
    task = queue.claim_task("w1")
    assert task is not None
    assert task.state == TaskState.LOCKED
    assert task.lock_owner == "w1"
    assert task.locked_at is not None
    # No other READY task -> a second claim gets nothing.
    assert queue.claim_task("w2") is None


def test_active_task_dedupe_one_per_inchikey():
    t1 = queue.enqueue_task(IK_A)
    t2 = queue.enqueue_task(IK_A)
    assert t1 is not None and t2 is not None
    assert t1.id == t2.id
    active = RemoteTask.objects.filter(
        inchikey=IK_A, state__in=[s.value for s in TaskState.active_states()]
    )
    assert active.count() == 1


def test_dedupe_allows_new_after_done():
    t1 = queue.enqueue_task(IK_A)
    assert t1 is not None
    queue.finish_task(t1)
    t2 = queue.enqueue_task(IK_A)
    assert t2 is not None and t2.id != t1.id


# ---------------------------------------------------------------------------
# stale-lock recovery
# ---------------------------------------------------------------------------


def test_stale_lock_recovery_resets_to_ready():
    task = queue.enqueue_task(IK_A)
    assert task is not None
    # Simulate a worker that claimed then died: LOCKED with an old locked_at.
    RemoteTask.objects.filter(id=task.id).update(
        state=TaskState.LOCKED,
        lock_owner="dead",
        locked_at=timezone.now() - dt.timedelta(hours=1),
    )
    recovered = queue.recover_stale_locks(lease_seconds=60)
    assert recovered == 1
    task.refresh_from_db()
    assert task.state == TaskState.READY
    assert task.lock_owner is None
    # And it can now be reclaimed.
    assert queue.claim_task("w-new") is not None


def test_fresh_lock_not_recovered():
    task = queue.enqueue_task(IK_A)
    assert task is not None
    RemoteTask.objects.filter(id=task.id).update(
        state=TaskState.LOCKED, lock_owner="alive", locked_at=timezone.now()
    )
    assert queue.recover_stale_locks(lease_seconds=300) == 0


# ---------------------------------------------------------------------------
# entity-first flow
# ---------------------------------------------------------------------------


def test_entity_found_persists_and_fans_out():
    job = _job()
    _item(job, IK_A, 0)
    _item(job, IK_A, 1)  # two items waiting on the same key
    queue.enqueue_task(IK_A)

    provider = MockProvider(entities={IK_A: build_raw(IK_A)})
    worker = _worker(provider)
    assert worker.tick() is True

    task = RemoteTask.objects.get(inchikey=IK_A)
    assert task.state == TaskState.DONE
    live = ClassyfireLive.objects.get(inchikey=IK_A)
    assert live.source_type == SourceType.WISHART_LIVE_ENTITY
    assert live.source_priority == 100
    items = BatchItem.objects.filter(job=job, inchikey=IK_A)
    assert all(i.status == BatchItemStatus.FOUND_WISHART_ENTITY for i in items)


def test_entity_miss_no_structure_sets_need_structure():
    job = _job()
    _item(job, IK_C, 0)  # no compound -> no structure
    queue.enqueue_task(IK_C)  # task without compound

    provider = MockProvider()  # MISS
    worker = _worker(provider)
    worker.tick()

    task = RemoteTask.objects.get(inchikey=IK_C)
    assert task.state == TaskState.DONE
    item = BatchItem.objects.get(job=job, inchikey=IK_C)
    assert item.status == BatchItemStatus.NEED_STRUCTURE


def test_entity_miss_with_structure_submits_query():
    job = _job()
    comp = _compound(IK_A)
    _item(job, IK_A, 0, compound=comp)
    queue.enqueue_task(IK_A, compound_id=comp.id)

    provider = MockProvider(submit_query_ids=["q-77"])  # entity MISS, submit ok
    worker = _worker(provider)
    worker.tick()

    task = RemoteTask.objects.get(inchikey=IK_A)
    assert task.state == TaskState.POLL_WAIT
    query = RemoteQuery.objects.get(remote_query_id="q-77")
    assert query.state == QueryState.SUBMITTED
    assert provider.submit_calls and provider.submit_calls[0][0][0] == IK_A


def test_batch_extras_are_claimed_under_skip_locked(settings):
    # With a batch size > 1, the primary task pulls in extra READY tasks. Each
    # extra MUST be claimed (LOCKED + this worker's lock_owner) so a concurrent
    # claim_task cannot also grab it — preserving the single-owner invariant.
    settings.REMOTE_BATCH_SIZE = 3
    comp_a = _compound(IK_A)
    comp_b = _compound(IK_B)
    comp_c = _compound(IK_C)
    queue.enqueue_task(IK_A, compound_id=comp_a.id)
    queue.enqueue_task(IK_B, compound_id=comp_b.id)
    queue.enqueue_task(IK_C, compound_id=comp_c.id)

    # The worker claims the primary task the normal way, then folds in extras.
    worker = _worker(MockProvider(), worker_id="owner")
    primary = queue.claim_task("owner")
    assert primary is not None

    extras = worker._claim_batch_extras(primary)

    # Both other READY tasks were claimed and are now owned by this worker.
    assert {e.inchikey for e in extras} == {IK_B, IK_C}
    for extra in extras:
        extra.refresh_from_db()
        assert extra.state == TaskState.LOCKED
        assert extra.lock_owner == "owner"
        assert extra.locked_at is not None

    # A concurrent claim now finds nothing READY to grab — no double-submit.
    assert queue.claim_task("rival") is None


def test_batch_extra_without_structure_is_not_claimed(settings):
    # An extra READY task lacking a submittable structure must be left untouched
    # (still READY, claimable) rather than locked and stranded.
    settings.REMOTE_BATCH_SIZE = 3
    comp_a = _compound(IK_A)
    queue.enqueue_task(IK_A, compound_id=comp_a.id)
    queue.enqueue_task(IK_B)  # no compound -> no structure

    worker = _worker(MockProvider(), worker_id="owner")
    primary = queue.claim_task("owner")
    assert primary is not None

    extras = worker._claim_batch_extras(primary)
    assert extras == []
    no_struct = RemoteTask.objects.get(inchikey=IK_B)
    assert no_struct.state == TaskState.READY
    assert no_struct.lock_owner is None


# ---------------------------------------------------------------------------
# poll flow
# ---------------------------------------------------------------------------


def test_poll_running_then_complete_fans_out():
    job = _job()
    comp = _compound(IK_A)
    _item(job, IK_A, 0, compound=comp)
    queue.enqueue_task(IK_A, compound_id=comp.id)

    raw = build_raw(IK_A)
    provider = MockProvider(
        submit_query_ids=["q-1"],
        poll_script={"q-1": ["RUNNING", raw]},
    )
    worker = _worker(provider)

    # 1) submit
    worker.tick()
    task = RemoteTask.objects.get(inchikey=IK_A)
    assert task.state == TaskState.POLL_WAIT

    # make it due, poll -> RUNNING
    RemoteTask.objects.filter(id=task.id).update(next_run_at=timezone.now())
    worker.tick()
    task.refresh_from_db()
    assert task.state == TaskState.POLL_WAIT
    assert RemoteQuery.objects.get(remote_query_id="q-1").state == QueryState.POLLING

    # make it due again, poll -> COMPLETE
    RemoteTask.objects.filter(id=task.id).update(next_run_at=timezone.now())
    worker.tick()
    task.refresh_from_db()
    assert task.state == TaskState.DONE
    live = ClassyfireLive.objects.get(inchikey=IK_A)
    assert live.source_type == SourceType.WISHART_LIVE_QUERY
    assert live.remote_query_id == "q-1"
    item = BatchItem.objects.get(job=job, inchikey=IK_A)
    assert item.status == BatchItemStatus.COMPLETE_AFTER_WISHART


# ---------------------------------------------------------------------------
# result-key mismatch guard
# ---------------------------------------------------------------------------


def test_result_key_mismatch_no_auto_apply():
    job = _job()
    _item(job, IK_A, 0)
    queue.enqueue_task(IK_A)

    # Entity FOUND but the payload carries a DIFFERENT key.
    provider = MockProvider(entities={IK_A: build_raw(IK_B)})
    worker = _worker(provider)
    worker.tick()

    task = RemoteTask.objects.get(inchikey=IK_A)
    assert task.state == TaskState.DONE
    # No live row written for the requested key.
    assert not ClassyfireLive.objects.filter(inchikey=IK_A).exists()
    item = BatchItem.objects.get(job=job, inchikey=IK_A)
    assert item.status == BatchItemStatus.RESULT_KEY_MISMATCH


# ---------------------------------------------------------------------------
# fan-out across jobs + counters
# ---------------------------------------------------------------------------


def test_fan_out_updates_all_waiting_items_across_jobs():
    job1 = _job()
    job2 = _job()
    _item(job1, IK_A, 0)
    _item(job1, IK_A, 1)
    _item(job2, IK_A, 0)
    queue.enqueue_task(IK_A)

    provider = MockProvider(entities={IK_A: build_raw(IK_A)})
    _worker(provider).tick()

    waiting = BatchItem.objects.filter(inchikey=IK_A)
    assert waiting.count() == 3
    assert all(i.status == BatchItemStatus.FOUND_WISHART_ENTITY for i in waiting)
    job1.refresh_from_db()
    assert job1.completed_count == 2
    assert job1.remote_pending_count == 0
    assert job1.status == BatchJobStatus.COMPLETE


# ---------------------------------------------------------------------------
# write-time precedence + history
# ---------------------------------------------------------------------------


def test_new_live_supersedes_and_archives_old():
    from apps.classifications.result_writer import write_live_result
    from apps.remote.mock import parse_wishart_payload

    write_live_result(
        IK_A,
        parse_wishart_payload(build_raw(IK_A, kingdom="Organic compounds")),
        source_type=SourceType.WISHART_LIVE_ENTITY,
    )
    write_live_result(
        IK_A,
        parse_wishart_payload(build_raw(IK_A, kingdom="Inorganic compounds")),
        source_type=SourceType.WISHART_LIVE_QUERY,
    )
    assert ClassyfireLive.objects.filter(inchikey=IK_A).count() == 1
    assert ClassificationHistory.objects.filter(inchikey=IK_A).count() == 1


def test_lower_priority_does_not_overwrite_live():
    from apps.classifications.result_writer import write_live_result
    from apps.remote.mock import parse_wishart_payload

    write_live_result(
        IK_A, parse_wishart_payload(build_raw(IK_A)),
        source_type=SourceType.WISHART_LIVE_ENTITY,
    )
    outcome = write_live_result(
        IK_A, parse_wishart_payload(build_raw(IK_A)),
        source_type=SourceType.ARCHIVED_WISHART_JSON,  # priority 90 < 100
    )
    assert outcome.applied is False
    assert ClassificationHistory.objects.filter(inchikey=IK_A).count() == 0


def test_duplicate_write_does_not_raise_or_lose_history():
    # Two same-priority writes for the same key must not raise IntegrityError and
    # must keep exactly one live row plus the archived prior row in history (the
    # upsert supersedes in place, with no delete+create no-row window).
    from apps.classifications.result_writer import write_live_result
    from apps.remote.mock import parse_wishart_payload

    first = write_live_result(
        IK_A,
        parse_wishart_payload(build_raw(IK_A, kingdom="Organic compounds")),
        source_type=SourceType.WISHART_LIVE_ENTITY,
    )
    assert first.applied is True
    # A second write at equal priority supersedes; the prior row is archived.
    second = write_live_result(
        IK_A,
        parse_wishart_payload(build_raw(IK_A, kingdom="Inorganic compounds")),
        source_type=SourceType.WISHART_LIVE_QUERY,
    )
    assert second.applied is True
    assert second.archived_history is True
    # Live row stays unique and updates in place (same primary key, no gap).
    live = ClassyfireLive.objects.get(inchikey=IK_A)
    assert live.id == first.live_id
    assert ClassyfireLive.objects.filter(inchikey=IK_A).count() == 1
    assert ClassificationHistory.objects.filter(inchikey=IK_A).count() == 1


def test_concurrent_create_race_falls_back_to_update(monkeypatch):
    # Simulate the lost-create race: our initial select_for_update sees no row
    # (a concurrent writer's row is not yet visible to us), so we attempt a
    # create that hits the unique index. The savepoint must absorb the
    # IntegrityError and re-run as an in-place update — no error escapes, the
    # prior row is archived, and the live row stays unique.
    from apps.classifications import result_writer
    from apps.classifications.result_writer import write_live_result
    from apps.remote.mock import parse_wishart_payload

    # The "concurrent" writer already committed a row for this key.
    write_live_result(
        IK_A,
        parse_wishart_payload(build_raw(IK_A, kingdom="Organic compounds")),
        source_type=SourceType.WISHART_LIVE_ENTITY,
    )

    # Force the first select_for_update lookup to behave as if the row is not
    # visible yet, so the create path runs and trips the unique violation.
    real_manager = result_writer.ClassyfireLive.objects
    real_select = real_manager.select_for_update
    state = {"hidden": True}

    def hiding_select(*a, **k):
        qs = real_select(*a, **k)
        if state["hidden"]:
            state["hidden"] = False
            return qs.none()  # matches nothing on the first lookup
        return qs

    monkeypatch.setattr(real_manager, "select_for_update", hiding_select)

    outcome = write_live_result(
        IK_A,
        parse_wishart_payload(build_raw(IK_A, kingdom="Inorganic compounds")),
        source_type=SourceType.WISHART_LIVE_QUERY,
    )
    assert outcome.applied is True
    assert outcome.archived_history is True
    assert ClassyfireLive.objects.filter(inchikey=IK_A).count() == 1
    assert ClassificationHistory.objects.filter(inchikey=IK_A).count() == 1


# ---------------------------------------------------------------------------
# retry / backoff / Retry-After
# ---------------------------------------------------------------------------


def test_retryable_error_schedules_retry_with_backoff():
    queue.enqueue_task(IK_A)
    provider = MockProvider(entity_retryable_for={IK_A})
    worker = _worker(provider)
    worker.tick()

    task = RemoteTask.objects.get(inchikey=IK_A)
    assert task.state == TaskState.RETRY_WAIT
    assert task.attempt == 1
    assert task.next_run_at is not None
    # First backoff is ~60s (+ up to 25% jitter); definitely in the future.
    delta = (task.next_run_at - timezone.now()).total_seconds()
    assert 50 < delta < 80


def test_retry_after_header_takes_precedence():
    queue.enqueue_task(IK_A)
    provider = MockProvider(entity_retryable_for={IK_A}, entity_retry_after=600.0)
    worker = _worker(provider)
    worker.tick()

    task = RemoteTask.objects.get(inchikey=IK_A)
    assert task.state == TaskState.RETRY_WAIT
    delta = (task.next_run_at - timezone.now()).total_seconds()
    # ~600s, NOT the 60s first-backoff: server Retry-After wins.
    assert 590 < delta < 610


def test_backoff_schedule_progression():
    # Deterministic ordering: base delays grow 60 < 300 < 1800 ...
    d0 = queue.backoff_seconds(0, IK_A)
    d1 = queue.backoff_seconds(1, IK_A)
    d2 = queue.backoff_seconds(2, IK_A)
    assert 60 <= d0 < 75
    assert 300 <= d1 < 375
    assert 1800 <= d2 < 2250
    # Jitter is deterministic: same inputs -> same output.
    assert queue.backoff_seconds(0, IK_A) == d0


def test_attempts_exhausted_defers_never_discards():
    task = queue.enqueue_task(IK_A)
    assert task is not None
    task.attempt = queue.MAX_ATTEMPTS - 1
    task.save(update_fields=["attempt"])
    queue.schedule_retry(task)
    task.refresh_from_db()
    assert task.state == TaskState.DEFERRED
    # Still active (holds dedup slot), not DONE/discarded.
    assert task.state in list(TaskState.active_states())


# ---------------------------------------------------------------------------
# circuit breaker
# ---------------------------------------------------------------------------


def test_circuit_opens_after_consecutive_failures():
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout=999)
    for _ in range(3):
        queue.enqueue_task(IK_A)  # ensure a task exists each tick
        provider = MockProvider(entity_retryable_for={IK_A})
        worker = _worker(provider, circuit=breaker)
        # reset the task to READY so it can be claimed each iteration
        RemoteTask.objects.filter(inchikey=IK_A).update(
            state=TaskState.READY, next_run_at=None
        )
        worker.tick()
    assert breaker.state == CircuitState.OPEN


def test_circuit_open_defers_task_local_lookup_survives():
    # Open circuit: remote calls are skipped, but the local lookup service still
    # works and the queue keeps the task (DEFERRED, not lost).
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=999)
    breaker.record_failure()  # trip it
    assert breaker.state == CircuitState.OPEN

    queue.enqueue_task(IK_A)
    provider = MockProvider(entities={IK_A: build_raw(IK_A)})
    worker = _worker(provider, circuit=breaker)
    worker.tick()

    task = RemoteTask.objects.get(inchikey=IK_A)
    assert task.state == TaskState.DEFERRED
    # No remote call was made.
    assert provider.entity_calls == []

    # Local lookup service still returns seeded data (NFR-P05).
    from apps.classifications.result_writer import write_live_result
    from apps.classifications.services import lookup_current_classification
    from apps.remote.mock import parse_wishart_payload

    write_live_result(
        IK_B, parse_wishart_payload(build_raw(IK_B)),
        source_type=SourceType.WISHART_LIVE_ENTITY,
    )
    assert lookup_current_classification(IK_B) is not None


def test_circuit_half_open_recovers_on_success():
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=0)
    breaker.record_failure()
    # reset_timeout=0 -> immediately HALF_OPEN.
    assert breaker.state == CircuitState.HALF_OPEN
    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# REMOTE_ENABLED = false
# ---------------------------------------------------------------------------


def test_remote_disabled_accumulates_without_calls(settings):
    settings.REMOTE_ENABLED = False
    job = _job()
    _item(job, IK_A, 0)
    queue.enqueue_task(IK_A)

    provider = MockProvider(entities={IK_A: build_raw(IK_A)})
    worker = _worker(provider)
    worker.tick()

    # No remote call made; task parked (DEFERRED), still active in queue.
    assert provider.entity_calls == []
    task = RemoteTask.objects.get(inchikey=IK_A)
    assert task.state == TaskState.DEFERRED
    # Item still waiting (not resolved).
    item = BatchItem.objects.get(job=job, inchikey=IK_A)
    assert item.status == BatchItemStatus.REMOTE_DELAYED
