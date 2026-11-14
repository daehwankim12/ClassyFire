from pathlib import Path


def test_backpressure_retry_locks_items_before_job_to_match_fanout_order():
    """Guard against reintroducing the BatchJob -> BatchItem deadlock order."""
    source = Path("apps/batches/services.py").read_text()
    start = source.index("def _retry_transiently_delayed_remote_items")
    end = source.index("class IngestLeaseLost", start)
    body = source[start:end]

    item_lock = body.index("BatchItem.objects.select_for_update()")
    job_lock = body.index("_lock_owned_job(claim)")

    assert item_lock < job_lock


def test_remote_fence_locks_generation_items_before_task():
    """Fenced result writes must use the same item -> task order as enrollment."""
    source = Path("apps/remote/queue.py").read_text()
    start = source.index("def lock_owned_claim")
    end = source.index("# Backoff schedule", start)
    body = source[start:end]

    item_lock = body.index("fanout.lock_task_items(claim.task_id)")
    task_lock = body.index("RemoteTask.objects.select_for_update()")

    assert item_lock < task_lock


def test_fanout_defers_job_locks_until_commit():
    """Counter recomputation must wait until task and item locks are released."""
    source = Path("apps/remote/fanout.py").read_text()
    start = source.index("def _recompute_jobs_after_commit")
    end = source.index("def mark_items_delayed", start)
    body = source[start:end]

    assert "transaction.on_commit(" in body
    assert "recompute_job_counters(job_id)" in body


def test_enrollment_rechecks_classification_after_task_lock():
    """A task that completed during lock wait must resolve from the live cache."""
    source = Path("apps/batches/services.py").read_text()
    start = source.index("def _get_or_create_remote_task")
    end = source.index("def _enroll_remote_item", start)
    body = source[start:end]

    task_lock = body.index("RemoteTask.objects.select_for_update(no_key=True)")
    live_recheck = body.rindex("lookup_current_classification(inchikey)")

    assert task_lock < live_recheck
    assert "raise _ClassificationBecameAvailableError(result)" in body[live_recheck:]


def test_enrollment_locks_tasks_without_blocking_fk_key_share():
    """Enrollment must not take FOR UPDATE on task rows.

    Workers insert RemoteAttempt/RemoteQueryItem rows referencing several task
    rows in one transaction, which takes FOR KEY SHARE on each. FOR UPDATE
    conflicts with KEY SHARE (deadlock with a chunk enrolling many keys);
    FOR NO KEY UPDATE does not.
    """
    source = Path("apps/batches/services.py").read_text()
    start = source.index("def _get_or_create_remote_task")
    end = source.index("\ndef ", start + 1)
    body = source[start:end]

    assert "RemoteTask.objects.select_for_update()" not in body
    assert body.count("RemoteTask.objects.select_for_update(no_key=True)") == 2


def test_deferred_job_recompute_is_robust_per_job():
    """One job's counter recompute failing must not skip the other jobs."""
    source = Path("apps/remote/fanout.py").read_text()
    start = source.index("def _recompute_jobs_after_commit")
    end = source.index("\ndef ", start + 1)
    body = source[start:end]

    assert "robust=True" in body
    assert "for job_id in ids:\n        transaction.on_commit(" in body
