from __future__ import annotations

import pytest
from django.utils import timezone

from apps.classifications.models import ClassificationResult
from apps.compounds.models import CompoundStructure
from apps.core.enums import QueryState, TaskState
from apps.remote.models import RemoteQuery, RemoteQueryItem, RemoteTask
from apps.remote.provider import ChemOntSlot, NormalizedClassification, PollResult, PollStatus
from apps.remote.worker import RemoteWorker


class CompletePollProvider:
    def poll_query(self, remote_query_id: str) -> PollResult:
        return PollResult(
            status=PollStatus.COMPLETE,
            raw_json={"entities": [{"inchikey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"}]},
            raw_text='{"entities":[{"inchikey":"LFQSCWFLJHTTHZ-UHFFFAOYSA-N"}]}',
        )

    def parse_results(self, raw_json: dict) -> list[NormalizedClassification]:
        return [
            NormalizedClassification(
                inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
                kingdom=ChemOntSlot(None, "Organic compounds"),
                superclass=ChemOntSlot(None, "Organic oxygen compounds"),
                class_=ChemOntSlot(None, "Organooxygen compounds"),
                subclass=None,
                direct_parent=None,
                raw_json=raw_json["entities"][0],
            )
        ]

    def lookup_entity(self, inchikey: str):
        raise AssertionError("entity lookup is not part of this test")

    def submit_structures(self, structures):
        raise AssertionError("submit is not part of this test")


class AllowAllLimiter:
    class Bucket:
        def allow(self) -> bool:
            return True

        def penalize(self, retry_after: float) -> None:
            self.last_retry_after = retry_after

    def __init__(self) -> None:
        self.entity = self.Bucket()
        self.submit = self.Bucket()


@pytest.mark.django_db(transaction=True)
def test_stale_poll_worker_cannot_borrow_fresh_lease(settings):
    settings.REMOTE_POLL_MIN_SECONDS = 1

    compound = CompoundStructure.objects.create(
        inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
        canonical_smiles="CCO",
        original_input="CCO",
    )
    task = RemoteTask.objects.create(
        inchikey=compound.inchikey,
        compound=compound,
        state=TaskState.LOCKED,
        lock_owner="stale-worker",
        locked_at=timezone.now(),
        lease_version=3,
    )
    query = RemoteQuery.objects.create(
        remote_query_id="Q123",
        payload_hash="hash123",
        state=QueryState.SUBMITTED,
        next_poll_at=timezone.now(),
    )
    RemoteQueryItem.objects.create(query=query, task=task, compound=compound, inchikey=compound.inchikey)

    stale_worker = RemoteWorker(
        CompletePollProvider(),
        worker_id="stale-worker",
        rate_limiter=AllowAllLimiter(),
        lease_seconds=0.1,
    )
    stale_claimed_task = RemoteTask.objects.get(pk=task.pk)

    RemoteTask.objects.filter(pk=task.pk).update(
        lock_owner="fresh-worker",
        lease_version=4,
        state=TaskState.LOCKED,
        locked_at=timezone.now(),
    )

    stale_worker.process(stale_claimed_task)

    task.refresh_from_db()
    query.refresh_from_db()
    assert task.state == TaskState.LOCKED
    assert task.lock_owner == "fresh-worker"
    assert query.state == QueryState.SUBMITTED
    assert ClassificationResult.objects.count() == 0
