"""Tests for the remote queue, worker, retry/backoff, circuit, and fan-out.

All tests run against the REAL Postgres DB using the deterministic MockProvider
(no network). They exercise the FOR UPDATE SKIP LOCKED claim, lock TTL recovery,
per-InChIKey dedupe, entity-first flow, /queries submit + poll, result-key
mismatch guard, fan-out to all waiting items, Retry-After precedence, backoff
schedule, circuit-breaker behaviour, and REMOTE_ENABLED=false.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from django.test import Client
from django.utils import timezone

from apps.batches.models import BatchItem, BatchJob
from apps.batches.services import create_job
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
from apps.remote.mock import MockProvider, build_query_response, build_raw
from apps.remote.models import RemoteQuery, RemoteTask
from apps.remote.rate_limit import RateLimiter
from apps.remote.worker import RemoteWorker

pytestmark = pytest.mark.django_db(transaction=True)

IK_A = "RDHQFKQIGNGIED-MRVPVSSYSA-N"
IK_B = "WQZGKKKJIJFFOK-GASJEMHNSA-N"
IK_C = "AAAABBBBCCCCDD-EEEEFFFFFF-N"
ETHANOL_INCHIKEY = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
ETHANOL_SMILES = "CCO"


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
          status=BatchItemStatus.PENDING_CLASSIFICATION) -> BatchItem:
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


def test_need_structure_then_submit_structure_and_complete_remote_calculation():
    job, secret = create_job(
        [{"raw": ETHANOL_INCHIKEY, "row_index": 0}],
        remote_enabled=True,
    )
    item = job.items.get()
    assert item.status == BatchItemStatus.PENDING_CLASSIFICATION
    assert item.compound_id is None

    provider = MockProvider(submit_query_ids=["q-need-structure"])
    worker = _worker(provider)
    assert worker.tick() is True

    item.refresh_from_db()
    assert item.status == BatchItemStatus.NEED_STRUCTURE
    assert RemoteTask.objects.get(inchikey=ETHANOL_INCHIKEY).state == TaskState.DONE

    client = Client()
    response = client.post(
        f"/api/items/{item.pk}/structure",
        data=json.dumps({"structure": ETHANOL_SMILES, "secret": secret}),
        content_type="application/json",
    )
    assert response.status_code == 200

    item.refresh_from_db()
    task = RemoteTask.objects.get(
        inchikey=ETHANOL_INCHIKEY,
        state__in=[TaskState.READY, TaskState.LOCKED, TaskState.POLL_WAIT],
    )
    assert item.status == BatchItemStatus.PENDING_CLASSIFICATION
    assert item.compound_id is not None
    assert task.compound_id == item.compound_id

    assert worker.tick() is True
    task.refresh_from_db()
    assert task.state == TaskState.POLL_WAIT

    provider.poll_script["q-need-structure"] = [
        build_query_response([build_raw(ETHANOL_INCHIKEY)])
    ]
    RemoteTask.objects.filter(pk=task.pk).update(next_run_at=timezone.now())
    assert worker.tick() is True

    item.refresh_from_db()
    task.refresh_from_db()
    assert task.state == TaskState.DONE
    assert item.status == BatchItemStatus.COMPLETE_AFTER_WISHART


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


def test_batch_submit_retryable_failure_retries_all_claimed_members(settings):
    # If a batched /queries submit fails before a RemoteQuery is created, every
    # task included in that submit must be released into retry. Extras have
    # already been LOCKED by _claim_batch_extras, so handling only the primary
    # task would strand the rest until stale-lock recovery.
    settings.REMOTE_BATCH_SIZE = 3
    job = _job()
    for row, ik in enumerate((IK_A, IK_B, IK_C)):
        comp = _compound(ik)
        _item(job, ik, row, compound=comp)
        queue.enqueue_task(ik, compound_id=comp.id)

    provider = MockProvider(submit_retryable=True)
    worker = _worker(provider)

    worker.tick()

    assert len(provider.submit_calls) == 1
    assert len(provider.submit_calls[0]) == 3
    for ik in (IK_A, IK_B, IK_C):
        task = RemoteTask.objects.get(inchikey=ik)
        assert task.state == TaskState.RETRY_WAIT, ik
        assert task.lock_owner is None, ik
        assert task.locked_at is None, ik
        assert task.attempt == 1, ik
        item = BatchItem.objects.get(job=job, inchikey=ik)
        assert item.status == BatchItemStatus.REMOTE_DELAYED, ik


def test_batch_submit_non_retryable_failure_invalidates_all_claimed_members(settings):
    # A non-retryable submit failure applies to the submitted payload. All tasks
    # folded into that payload must reach terminal INVALID_INPUT instead of
    # leaving extras LOCKED/PENDING_CLASSIFICATION.
    settings.REMOTE_BATCH_SIZE = 3
    job = _job()
    for row, ik in enumerate((IK_A, IK_B, IK_C)):
        comp = _compound(ik)
        _item(job, ik, row, compound=comp)
        queue.enqueue_task(ik, compound_id=comp.id)

    provider = MockProvider(submit_non_retryable=True)
    worker = _worker(provider)

    worker.tick()

    assert len(provider.submit_calls) == 1
    assert len(provider.submit_calls[0]) == 3
    for ik in (IK_A, IK_B, IK_C):
        task = RemoteTask.objects.get(inchikey=ik)
        assert task.state == TaskState.DONE, ik
        assert task.lock_owner is None, ik
        assert task.locked_at is None, ik
        item = BatchItem.objects.get(job=job, inchikey=ik)
        assert item.status == BatchItemStatus.INVALID_INPUT, ik


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
# multi-result batched query (REMOTE_BATCH_SIZE > 1)
# ---------------------------------------------------------------------------


def _drain_batch_query(provider, worker) -> None:
    """Submit one batch, then poll it to COMPLETE (no re-submit afterwards).

    Only POLL_WAIT tasks are made due so the single batched query is polled to
    completion; tasks that revert to retry after the query completes are left
    parked (the tests assert on that parked state) rather than re-submitted.
    """
    # 1) submit: the primary task pulls in extras -> one query for the batch.
    worker.tick()
    # 2) poll the batched query until it is COMPLETE.
    for _ in range(10):
        if RemoteQuery.objects.filter(state=QueryState.COMPLETE).exists():
            break
        RemoteTask.objects.filter(state=TaskState.POLL_WAIT).update(
            next_run_at=timezone.now()
        )
        if not worker.tick():
            break


def test_multi_result_batch_resolves_every_task(settings):
    # THE CORE BUG: a /queries submission batches up to REMOTE_BATCH_SIZE
    # structures into ONE query; the completed response carries one entity per
    # structure. EVERY batched task must be resolved by its own InChIKey — not
    # just entities[0]. On the old entities[0]-only code IK_B and IK_C would
    # never be resolved (they would fall out of polling), so this test is RED
    # before the fix.
    settings.REMOTE_BATCH_SIZE = 3
    job = _job()
    for row, ik in enumerate((IK_A, IK_B, IK_C)):
        comp = _compound(ik)
        _item(job, ik, row, compound=comp)
        queue.enqueue_task(ik, compound_id=comp.id)

    response = build_query_response(
        [build_raw(IK_A), build_raw(IK_B), build_raw(IK_C)]
    )
    provider = MockProvider(
        submit_query_ids=["q-batch"],
        poll_script={"q-batch": [response]},
    )
    worker = _worker(provider)

    # Exactly ONE query was submitted for the whole batch.
    worker.tick()
    assert len(provider.submit_calls) == 1
    assert len(provider.submit_calls[0]) == 3
    assert RemoteQuery.objects.count() == 1

    # Poll the single query to completion.
    for _ in range(10):
        if RemoteQuery.objects.filter(state=QueryState.COMPLETE).exists():
            break
        RemoteTask.objects.filter(state=TaskState.POLL_WAIT).update(
            next_run_at=timezone.now()
        )
        if not worker.tick():
            break

    # ALL three tasks DONE, all three live results persisted, all items fanned.
    for ik in (IK_A, IK_B, IK_C):
        task = RemoteTask.objects.get(inchikey=ik)
        assert task.state == TaskState.DONE, ik
        live = ClassyfireLive.objects.get(inchikey=ik)
        assert live.source_type == SourceType.WISHART_LIVE_QUERY
        assert live.source_priority == 100
        assert live.remote_query_id == "q-batch"
        item = BatchItem.objects.get(job=job, inchikey=ik)
        assert item.status == BatchItemStatus.COMPLETE_AFTER_WISHART
    # No task left stranded in a poll-waiting state.
    assert not RemoteTask.objects.filter(
        state__in=[TaskState.SUBMITTED, TaskState.POLL_WAIT]
    ).exists()
    assert RemoteQuery.objects.get(remote_query_id="q-batch").state == (
        QueryState.COMPLETE
    )


def test_partial_response_resolves_present_and_retries_absent(settings):
    # The query returns only 2 of 3 entities. The two present are resolved; the
    # absent one is NOT stranded — it retries per the normal backoff rules.
    settings.REMOTE_BATCH_SIZE = 3
    job = _job()
    for row, ik in enumerate((IK_A, IK_B, IK_C)):
        comp = _compound(ik)
        _item(job, ik, row, compound=comp)
        queue.enqueue_task(ik, compound_id=comp.id)

    # IK_C is missing from the response.
    response = build_query_response([build_raw(IK_A), build_raw(IK_B)])
    provider = MockProvider(
        submit_query_ids=["q-batch"],
        poll_script={"q-batch": [response]},
    )
    worker = _worker(provider)
    _drain_batch_query(provider, worker)

    for ik in (IK_A, IK_B):
        assert RemoteTask.objects.get(inchikey=ik).state == TaskState.DONE
        assert ClassyfireLive.objects.filter(inchikey=ik).exists()
        assert BatchItem.objects.get(job=job, inchikey=ik).status == (
            BatchItemStatus.COMPLETE_AFTER_WISHART
        )

    # The absent task is retried (or deferred), never stranded / DONE-without-result.
    absent = RemoteTask.objects.get(inchikey=IK_C)
    assert absent.state in {
        TaskState.RETRY_WAIT,
        TaskState.READY,
        TaskState.DEFERRED,
    }
    assert absent.attempt >= 1
    assert not ClassyfireLive.objects.filter(inchikey=IK_C).exists()
    assert BatchItem.objects.get(job=job, inchikey=IK_C).status == (
        BatchItemStatus.REMOTE_DELAYED
    )


def test_mismatch_key_in_batch_marks_offending_task(settings):
    # A batched task whose response entity carries a DIFFERENT key (but the key
    # IS present in the response, mapped to that slot) -> RESULT_KEY_MISMATCH for
    # that task only; the others resolve normally.
    settings.REMOTE_BATCH_SIZE = 2
    job = _job()
    for row, ik in enumerate((IK_A, IK_B)):
        comp = _compound(ik)
        _item(job, ik, row, compound=comp)
        queue.enqueue_task(ik, compound_id=comp.id)

    # We patch parse_results so the IK_B task is handed an entity whose key !=
    # its requested key (mismatch path), while IK_A matches.
    from apps.remote import mock as mock_mod

    response = build_query_response([build_raw(IK_A), build_raw(IK_B)])
    provider = MockProvider(
        submit_query_ids=["q-batch"],
        poll_script={"q-batch": [response]},
    )

    real_parse = mock_mod.parse_wishart_results

    def mismatched(raw):
        norms = real_parse(raw)
        out = []
        for n in norms:
            if n.inchikey == IK_B:
                out.append(
                    mock_mod.NormalizedClassification(
                        inchikey=IK_C,  # wrong key reported for the IK_B slot
                        kingdom=n.kingdom,
                        superclass=n.superclass,
                        class_=n.class_,
                        subclass=n.subclass,
                        direct_parent=n.direct_parent,
                        classification_version=n.classification_version,
                        raw_json=n.raw_json,
                        alternative_parents=n.alternative_parents,
                    )
                )
            else:
                out.append(n)
        return out

    provider.parse_results = mismatched  # type: ignore[method-assign]
    worker = _worker(provider)
    _drain_batch_query(provider, worker)

    assert RemoteTask.objects.get(inchikey=IK_A).state == TaskState.DONE
    assert ClassyfireLive.objects.filter(inchikey=IK_A).exists()

    # IK_B: response carried a non-matching key -> mismatch, terminal, no live row.
    b_task = RemoteTask.objects.get(inchikey=IK_B)
    assert b_task.state == TaskState.DONE
    assert not ClassyfireLive.objects.filter(inchikey=IK_B).exists()
    assert BatchItem.objects.get(job=job, inchikey=IK_B).status == (
        BatchItemStatus.RESULT_KEY_MISMATCH
    )


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


def test_result_writer_rejects_returned_key_mismatch():
    from apps.classifications.result_writer import write_live_result
    from apps.remote.mock import parse_wishart_payload

    outcome = write_live_result(
        IK_A,
        parse_wishart_payload(build_raw(IK_B)),
        source_type=SourceType.WISHART_LIVE_ENTITY,
    )

    assert outcome.applied is False
    assert outcome.reason == "result key mismatch"
    assert not ClassyfireLive.objects.filter(inchikey=IK_A).exists()
    assert not ClassyfireLive.objects.filter(inchikey=IK_B).exists()


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


def test_every_resolution_path_pins_classification_snapshot():
    from apps.chemont.models import ChemOntNode
    from apps.classifications.models import ClassyfireSeed

    live_node = ChemOntNode.objects.create(
        numeric_id=9100,
        chemont_id="CHEMONT:0009100",
        name="Live pinned kingdom",
        ontology_version="2.1",
    )
    seed_node = ChemOntNode.objects.create(
        numeric_id=9101,
        chemont_id="CHEMONT:0009101",
        name="Seed pinned kingdom",
        ontology_version="2.1",
    )
    ClassyfireLive.objects.create(
        inchikey=IK_A,
        source_type=SourceType.WISHART_LIVE_QUERY,
        source_priority=100,
        retrieved_at=dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC),
        kingdom_id=live_node.numeric_id,
    )
    ClassyfireSeed.objects.create(
        inchikey=IK_B,
        inchikey14=IK_B[:14],
        seed_source_id=1,
        seed_version="fixture",
        kingdom_id=seed_node.numeric_id,
    )

    local_job, _secret = create_job(
        [
            {"raw": IK_A, "row_index": 0},
            {"raw": IK_B, "row_index": 1},
        ],
        remote_enabled=True,
    )
    live_item = local_job.items.get(row_order=0)
    seed_item = local_job.items.get(row_order=1)
    assert live_item.status == BatchItemStatus.FOUND_LIVE_CACHE
    assert live_item.classification_snapshot is not None
    assert live_item.classification_snapshot["inchikey"] == IK_A
    assert live_item.classification_snapshot["tier"] == "LIVE"
    assert live_item.classification_snapshot["source_type"] == (
        SourceType.WISHART_LIVE_QUERY
    )
    assert live_item.classification_snapshot["kingdom"]["name"] == (
        "Live pinned kingdom"
    )
    assert seed_item.status == BatchItemStatus.FOUND_SEED_CACHE
    assert seed_item.classification_snapshot is not None
    assert seed_item.classification_snapshot["inchikey"] == IK_B
    assert seed_item.classification_snapshot["tier"] == "SEED"
    assert seed_item.classification_snapshot["kingdom"]["name"] == (
        "Seed pinned kingdom"
    )

    entity_job = _job()
    _item(entity_job, IK_C, 0)
    queue.enqueue_task(IK_C)
    entity_worker = _worker(MockProvider(entities={IK_C: build_raw(IK_C)}))
    assert entity_worker.tick() is True
    entity_item = BatchItem.objects.get(job=entity_job, inchikey=IK_C)
    assert entity_item.status == BatchItemStatus.FOUND_WISHART_ENTITY
    assert entity_item.classification_snapshot is not None
    assert entity_item.classification_snapshot["inchikey"] == IK_C
    assert entity_item.classification_snapshot["tier"] == "LIVE"
    assert entity_item.classification_snapshot["source_type"] == (
        SourceType.WISHART_LIVE_ENTITY
    )
    assert entity_item.classification_snapshot["kingdom"]["name"] == (
        "Organic compounds"
    )

    query_key = "CCCCCCCCCCCCCC-DDDDDDDDDD-N"
    query_job = _job()
    query_compound = _compound(query_key)
    _item(query_job, query_key, 0, compound=query_compound)
    queue.enqueue_task(query_key, compound_id=query_compound.id)
    query_worker = _worker(
        MockProvider(
            submit_query_ids=["q-snapshot"],
            poll_script={"q-snapshot": [build_query_response([build_raw(query_key)])]},
        )
    )
    assert query_worker.tick() is True
    RemoteTask.objects.filter(inchikey=query_key).update(next_run_at=timezone.now())
    assert query_worker.tick() is True
    query_item = BatchItem.objects.get(job=query_job, inchikey=query_key)
    assert query_item.status == BatchItemStatus.COMPLETE_AFTER_WISHART
    assert query_item.classification_snapshot is not None
    assert query_item.classification_snapshot["inchikey"] == query_key
    assert query_item.classification_snapshot["tier"] == "LIVE"
    assert query_item.classification_snapshot["source_type"] == (
        SourceType.WISHART_LIVE_QUERY
    )
    assert query_item.classification_snapshot["query_id"] == "q-snapshot"

    structure_job, structure_secret = create_job(
        [{"raw": ETHANOL_INCHIKEY, "row_index": 0}],
        remote_enabled=True,
    )
    structure_item = structure_job.items.get()
    structure_worker = _worker(MockProvider(submit_query_ids=["q-structure-snapshot"]))
    assert structure_worker.tick() is True
    structure_item.refresh_from_db()
    assert structure_item.status == BatchItemStatus.NEED_STRUCTURE

    client = Client()
    response = client.post(
        f"/api/items/{structure_item.pk}/structure",
        data=json.dumps({"structure": ETHANOL_SMILES, "secret": structure_secret}),
        content_type="application/json",
    )
    assert response.status_code == 200

    assert structure_worker.tick() is True
    structure_task = RemoteTask.objects.get(
        inchikey=ETHANOL_INCHIKEY,
        state=TaskState.POLL_WAIT,
    )
    structure_worker.provider.poll_script["q-structure-snapshot"] = [
        build_query_response([build_raw(ETHANOL_INCHIKEY)])
    ]
    RemoteTask.objects.filter(pk=structure_task.pk).update(next_run_at=timezone.now())
    assert structure_worker.tick() is True
    structure_item.refresh_from_db()
    assert structure_item.status == BatchItemStatus.COMPLETE_AFTER_WISHART
    assert structure_item.classification_snapshot is not None
    assert structure_item.classification_snapshot["inchikey"] == ETHANOL_INCHIKEY
    assert structure_item.classification_snapshot["tier"] == "LIVE"
    assert structure_item.classification_snapshot["source_type"] == (
        SourceType.WISHART_LIVE_QUERY
    )
    assert structure_item.classification_snapshot["query_id"] == (
        "q-structure-snapshot"
    )


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


def test_equal_priority_older_result_does_not_overwrite_newer_live():
    from apps.classifications.result_writer import write_live_result
    from apps.remote.mock import parse_wishart_payload

    newer = dt.datetime(2026, 1, 2, 12, 0, tzinfo=dt.UTC)
    older = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)

    first = write_live_result(
        IK_A,
        parse_wishart_payload(
            build_raw(IK_A, kingdom="Organic compounds", version="newer")
        ),
        source_type=SourceType.WISHART_LIVE_QUERY,
        retrieved_at=newer,
    )
    second = write_live_result(
        IK_A,
        parse_wishart_payload(
            build_raw(IK_A, kingdom="Inorganic compounds", version="older")
        ),
        source_type=SourceType.WISHART_LIVE_QUERY,
        retrieved_at=older,
    )

    live = ClassyfireLive.objects.get(inchikey=IK_A)
    assert first.applied is True
    assert second.applied is False
    assert second.reason == "existing same priority is newer or equal"
    assert live.classification_version == "newer"
    assert live.retrieved_at == newer
    assert ClassificationHistory.objects.filter(inchikey=IK_A).count() == 0


def test_chemont_name_collision_does_not_link_wrong_node():
    from apps.chemont.models import ChemOntNode
    from apps.classifications.result_writer import write_live_result
    from apps.remote.provider import ChemOntSlot, NormalizedClassification

    ChemOntNode.objects.create(
        numeric_id=9200,
        chemont_id="CHEMONT:0009200",
        name="Ambiguous ChemOnt label",
        ontology_version="2.1",
    )
    ChemOntNode.objects.create(
        numeric_id=9201,
        chemont_id="CHEMONT:0009201",
        name="Ambiguous ChemOnt label",
        ontology_version="2.1",
    )
    norm = NormalizedClassification(
        inchikey=IK_A,
        kingdom=ChemOntSlot(chemont_id=None, name="Ambiguous ChemOnt label"),
        superclass=None,
        class_=None,
        subclass=None,
        direct_parent=None,
        raw_json={"inchikey": IK_A},
    )

    outcome = write_live_result(
        IK_A,
        norm,
        source_type=SourceType.WISHART_LIVE_ENTITY,
    )

    assert outcome.applied is True
    live = ClassyfireLive.objects.get(inchikey=IK_A)
    assert live.kingdom_id is None


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


# ---------------------------------------------------------------------------
# Fix #3: defer/retry surfaces REMOTE_DELAYED; later success → COMPLETE_AFTER_WISHART
# ---------------------------------------------------------------------------


def test_retry_transitions_pending_to_remote_delayed():
    """When a task is parked for retry (retryable error), waiting items must
    transition from PENDING_CLASSIFICATION to REMOTE_DELAYED so the user-facing
    status is immediately surfaced (spec §6.2)."""
    job = _job()
    item = _item(job, IK_A, 0)  # starts PENDING_CLASSIFICATION (new default)
    assert item.status == BatchItemStatus.PENDING_CLASSIFICATION

    queue.enqueue_task(IK_A)
    provider = MockProvider(entity_retryable_for={IK_A})
    worker = _worker(provider)
    worker.tick()

    task = RemoteTask.objects.get(inchikey=IK_A)
    assert task.state == TaskState.RETRY_WAIT, (
        "Task must be in RETRY_WAIT after a retryable error"
    )
    # The waiting item must now show REMOTE_DELAYED.
    item.refresh_from_db()
    assert item.status == BatchItemStatus.REMOTE_DELAYED, (
        f"Waiting item must be REMOTE_DELAYED after retry, got {item.status}"
    )


def test_circuit_open_defer_transitions_pending_to_remote_delayed():
    """When the circuit is open, the task is deferred; waiting items must
    transition from PENDING_CLASSIFICATION to REMOTE_DELAYED."""
    from apps.remote.circuit import CircuitBreaker

    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=999)
    breaker.record_failure()  # trip the circuit open

    job = _job()
    item = _item(job, IK_A, 0)  # starts PENDING_CLASSIFICATION
    assert item.status == BatchItemStatus.PENDING_CLASSIFICATION

    queue.enqueue_task(IK_A)
    provider = MockProvider(entities={IK_A: build_raw(IK_A)})
    worker = _worker(provider, circuit=breaker)
    worker.tick()

    task = RemoteTask.objects.get(inchikey=IK_A)
    assert task.state == TaskState.DEFERRED, (
        "Task must be DEFERRED when circuit is open"
    )
    item.refresh_from_db()
    assert item.status == BatchItemStatus.REMOTE_DELAYED, (
        f"Waiting item must be REMOTE_DELAYED after circuit-open defer, got {item.status}"
    )


def test_subsequent_success_after_remote_delayed_completes_item():
    """After items are moved to REMOTE_DELAYED (due to a retry), a subsequent
    successful entity result must move them to COMPLETE_AFTER_WISHART (or
    FOUND_WISHART_ENTITY) — verifying that REMOTE_DELAYED items are still in
    the WAITING_STATUSES fan-out set."""
    job = _job()
    # Pre-set the item to REMOTE_DELAYED (simulating a prior retry cycle).
    item = _item(job, IK_A, 0, status=BatchItemStatus.REMOTE_DELAYED)
    assert item.status == BatchItemStatus.REMOTE_DELAYED

    queue.enqueue_task(IK_A)
    # Now the entity lookup succeeds.
    provider = MockProvider(entities={IK_A: build_raw(IK_A)})
    worker = _worker(provider)
    worker.tick()

    task = RemoteTask.objects.get(inchikey=IK_A)
    assert task.state == TaskState.DONE, (
        "Task must be DONE after a successful entity lookup"
    )
    item.refresh_from_db()
    assert item.status == BatchItemStatus.FOUND_WISHART_ENTITY, (
        f"REMOTE_DELAYED item must be resolved to FOUND_WISHART_ENTITY on success, "
        f"got {item.status}"
    )
