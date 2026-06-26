from pathlib import Path


def test_backpressure_retry_locks_items_before_job_to_match_fanout_order():
    """Guard against reintroducing the BatchJob -> BatchItem deadlock order."""
    source = Path("apps/batches/services.py").read_text()
    start = source.index("def _retry_backpressured_remote_delayed_items")
    end = source.index("class IngestLeaseLost", start)
    body = source[start:end]

    item_lock = body.index("BatchItem.objects.select_for_update()")
    job_lock = body.index("_lock_owned_job(claim)")

    assert item_lock < job_lock
