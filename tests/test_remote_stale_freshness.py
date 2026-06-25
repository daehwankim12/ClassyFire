"""Task 15 / F-18: an OLDER query completing LATE must not supersede a NEWER one.

Two remote QUERY results land at the SAME source priority
(``WISHART_LIVE_QUERY`` = 100). The apply timestamp is each query's
``submitted_at`` (NOT wall-clock at apply time), so when the OLDER query's result
is applied AFTER the NEWER query's, the result writer rejects it as
``REJECTED_STALE`` and the live pointer keeps the newer query.

The harness drives the REAL fenced apply (``RemoteWorker._apply_result_with_fence``
→ ``write_live_result``), the same path the poll/apply flow uses, so the freshness
guard is exercised through production code rather than a hand-rolled writer call.
It builds the initial RemoteQuery + RemoteTask fixtures directly only.
"""
from __future__ import annotations

import datetime as dt

import pytest

from apps.classifications.models import ClassificationResult, ClassyfireLive
from apps.compounds.models import CompoundStructure
from apps.core.enums import BatchItemStatus, ResultKind, SourceType, TaskState
from apps.remote import queue
from apps.remote.circuit import CircuitBreaker
from apps.remote.mock import build_raw, parse_wishart_payload
from apps.remote.models import RemoteQuery, RemoteTask
from apps.remote.rate_limit import RateLimiter
from apps.remote.worker import RemoteWorker

IK = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"


class _RemoteResultHarness:
    """Apply a remote QUERY result for a given query through real worker code."""

    def __init__(self) -> None:
        self.provider_calls = 0

    def _worker(self) -> RemoteWorker:
        from apps.remote.mock import MockProvider

        return RemoteWorker(
            MockProvider(),
            worker_id="w-apply",
            rate_limiter=RateLimiter(1000, 1000),
            circuit=CircuitBreaker(failure_threshold=1000),
        )

    def apply_query_result(self, query: RemoteQuery, inchikey: str, **labels) -> None:
        """Resolve ``inchikey`` for ``query`` via the real fenced apply path.

        Builds a fresh owned RemoteTask for the key, then drives
        ``_apply_result_with_fence`` exactly as ``_dispatch_resolved`` does for a
        clean query result — stamping ``retrieved_at=query.submitted_at`` so the
        freshness guard compares the two queries by their submit order.
        """
        norm = parse_wishart_payload(build_raw(inchikey, **labels))
        compound, _ = CompoundStructure.objects.get_or_create(
            inchikey=inchikey,
            defaults={"original_input": "CCO", "canonical_smiles": "CCO"},
        )
        # One active task per InChIKey (dedup) — create only when none is active.
        task = RemoteTask.objects.create(
            inchikey=inchikey, compound=compound, state=TaskState.READY
        )
        owned = queue.claim_task("w-apply")
        assert owned is not None and owned.pk == task.pk
        worker = self._worker()
        worker._apply_result_with_fence(
            queue.claim_from_task(owned),
            query=query,
            norm=norm,
            final_status=BatchItemStatus.COMPLETE_AFTER_WISHART,
        )


@pytest.fixture
def remote_result_harness():
    return _RemoteResultHarness()


def _query_with_submitted_at(
    remote_query_id: str, payload_hash: str, submitted_at: dt.datetime
) -> RemoteQuery:
    query = RemoteQuery.objects.create(
        remote_query_id=remote_query_id,
        payload_hash=payload_hash,
    )
    RemoteQuery.objects.filter(pk=query.pk).update(submitted_at=submitted_at)
    query.refresh_from_db(fields=["submitted_at"])
    return query


@pytest.mark.django_db(transaction=True)
def test_older_query_completing_late_does_not_supersede_newer_result(
    remote_result_harness,
):
    older = _query_with_submitted_at(
        "older", "olderhash", dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    )
    newer = _query_with_submitted_at(
        "newer", "newerhash", dt.datetime(2026, 1, 2, tzinfo=dt.UTC)
    )
    assert older.submitted_at < newer.submitted_at

    remote_result_harness.apply_query_result(newer, IK, kingdom="Organic compounds")
    remote_result_harness.apply_query_result(older, IK, kingdom="Organic compounds")

    live = ClassyfireLive.objects.get(inchikey=IK)
    assert live.remote_query_id == "newer"
    stale_event = ClassificationResult.objects.latest("id")
    assert stale_event.kind == ResultKind.REJECTED_STALE
    assert stale_event.source_type == SourceType.WISHART_LIVE_QUERY


@pytest.mark.django_db(transaction=True)
def test_newer_query_completing_after_older_supersedes_live_result(
    remote_result_harness,
):
    older = _query_with_submitted_at(
        "older", "olderhash", dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    )
    newer = _query_with_submitted_at(
        "newer", "newerhash", dt.datetime(2026, 1, 2, tzinfo=dt.UTC)
    )
    assert older.submitted_at < newer.submitted_at

    remote_result_harness.apply_query_result(older, IK, kingdom="Organic compounds")
    remote_result_harness.apply_query_result(newer, IK, kingdom="Organic compounds")

    live = ClassyfireLive.objects.get(inchikey=IK)
    assert live.remote_query_id == "newer"
    newer_event = ClassificationResult.objects.latest("id")
    assert newer_event.kind != ResultKind.REJECTED_STALE
    assert newer_event.source_type == SourceType.WISHART_LIVE_QUERY
