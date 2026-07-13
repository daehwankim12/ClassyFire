from __future__ import annotations

import httpx
import pytest
from django.utils import timezone

from apps.compounds.models import CompoundStructure
from apps.core.enums import SubmissionState, TaskState
from apps.remote.mock import MockProvider
from apps.remote.models import RemoteSubmission, RemoteSubmissionMember, RemoteTask
from apps.remote.provider import AmbiguousSubmissionError
from apps.remote.services import fail_unknown_submission
from apps.remote.wishart import WishartDirectProvider
from apps.remote.worker import RemoteWorker


class AcceptedThenReadErrorTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.submit_count = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.startswith("/entities/"):
            # Genuine entity miss (404) so the worker proceeds to a structure
            # submit — that POST is where the ambiguous transport error happens.
            return httpx.Response(404, request=request)
        if request.method == "POST" and request.url.path == "/queries.json":
            self.submit_count += 1
            raise httpx.ReadError("response lost after provider accepted", request=request)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")


class NoSleepLimiter:
    class Bucket:
        def allow(self) -> bool:
            return True

        def penalize(self, retry_after: float) -> None:
            self.last_retry_after = retry_after

    def __init__(self) -> None:
        self.entity = self.Bucket()
        self.submit = self.Bucket()


class ClosedCircuit:
    def __init__(self) -> None:
        self.success_count = 0
        self.failure_count = 0

    def allow(self) -> bool:
        return True

    def record_success(self) -> None:
        self.success_count += 1
        self.last = "success"

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last = "failure"


@pytest.mark.django_db(transaction=True)
def test_actual_wishart_submit_read_error_parks_unknown_without_repost(settings):
    settings.REMOTE_ENABLED = True
    settings.REMOTE_BATCH_SIZE = 1
    settings.REMOTE_SUBMISSION_RECONCILE_MAX = 1
    settings.REMOTE_SUBMISSION_TTL_SECONDS = 1
    settings.REMOTE_POLL_MIN_SECONDS = 1

    compound = CompoundStructure.objects.create(
        inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
        canonical_smiles="CCO",
        original_input="CCO",
    )
    task = RemoteTask.objects.create(
        inchikey=compound.inchikey,
        compound=compound,
        state=TaskState.READY,
        next_run_at=timezone.now(),
    )

    transport = AcceptedThenReadErrorTransport()
    provider = WishartDirectProvider(base_url="https://wishart.invalid")
    provider._http_client = httpx.Client(transport=transport, base_url="https://wishart.invalid")
    circuit = ClosedCircuit()
    worker = RemoteWorker(
        provider,
        worker_id="worker-a",
        rate_limiter=NoSleepLimiter(),
        circuit=circuit,
        lease_seconds=0.1,
    )

    assert worker.tick() is True
    assert transport.submit_count == 1
    submission = RemoteSubmission.objects.get()
    assert submission.state == SubmissionState.UNKNOWN

    task.refresh_from_db()
    assert task.state == TaskState.DEFERRED
    assert task.next_run_at is None
    assert circuit.failure_count == 0
    assert circuit.success_count == 2  # entity miss + ambiguous submit

    for _ in range(5):
        assert worker.tick() is False

    assert transport.submit_count == 1
    submission.refresh_from_db()
    assert submission.state == SubmissionState.UNKNOWN
    fail_unknown_submission(submission, actor="test")
    task.refresh_from_db()
    assert task.state == TaskState.READY
    assert task.next_run_at is not None


class AmbiguousSubmitProvider(MockProvider):
    """A provider whose every /queries POST ends in ambiguous acceptance.

    Records the (label, structure) batch it was asked to submit (so a test can
    assert which members were POSTed), then raises
    :class:`AmbiguousSubmissionError` so the submission is parked UNKNOWN — a LIVE
    state that holds the per-member reservation. Entity lookups MISS (empty
    ``entities``) so the worker routes to a structure submit.
    """

    def submit_structures(self, structures):  # type: ignore[override]
        self.submit_calls.append(list(structures))
        raise AmbiguousSubmissionError("provider acceptance unknown (test)")


def _seed_compound(inchikey: str, smiles: str) -> CompoundStructure:
    return CompoundStructure.objects.create(
        inchikey=inchikey,
        canonical_smiles=smiles,
        original_input=smiles,
    )


@pytest.mark.django_db(transaction=True)
def test_ambiguous_submission_member_cannot_repost_in_different_batch(settings):
    settings.REMOTE_ENABLED = True
    settings.REMOTE_BATCH_SIZE = 2
    settings.REMOTE_SUBMISSION_RECONCILE_MAX = 0
    settings.REMOTE_SUBMISSION_TTL_SECONDS = 0
    settings.REMOTE_POLL_MIN_SECONDS = 1

    a = _seed_compound("AAAAAAAAAAAAAA-BBBBBBBBBB-C", "C")
    b = _seed_compound("BBBBBBBBBBBBBB-CCCCCCCCCC-D", "CC")
    c = _seed_compound("CCCCCCCCCCCCCC-DDDDDDDDDD-E", "CCC")

    for structure in (a, b):
        RemoteTask.objects.create(
            inchikey=structure.inchikey,
            compound=structure,
            state=TaskState.READY,
        )

    provider = AmbiguousSubmitProvider()
    worker = RemoteWorker(
        provider,
        worker_id="regroup-worker",
        rate_limiter=NoSleepLimiter(),
        circuit=ClosedCircuit(),
        lease_seconds=0.1,
    )
    worker.tick()

    RemoteTask.objects.filter(inchikey=b.inchikey).update(state=TaskState.DONE)
    RemoteTask.objects.create(
        inchikey=c.inchikey,
        compound=c,
        state=TaskState.READY,
    )
    RemoteTask.objects.filter(inchikey=a.inchikey).update(
        state=TaskState.READY,
        lock_owner=None,
        locked_at=None,
        next_run_at=None,
    )

    worker.tick()

    posted_members = [
        inchikey
        for call in provider.submit_calls
        for inchikey, _structure in call
    ]
    assert posted_members.count(a.inchikey) == 1
    assert posted_members.count(c.inchikey) == 1
    assert (
        RemoteSubmissionMember.objects.filter(
            provider="wishart",
            inchikey=a.inchikey,
            active=True,
        ).count()
        == 1
    )


class Accepted2xxBodyTransport(httpx.BaseTransport):
    """A 200 on submit whose body is supplied by the test.

    Models a provider that ACCEPTED the POST (2xx) but returned a body the
    client cannot parse into a query id — AMBIGUOUS ACCEPTANCE (F-06).
    """

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.submit_count = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.startswith("/entities/"):
            return httpx.Response(404, request=request)
        if request.method == "POST" and request.url.path == "/queries.json":
            self.submit_count += 1
            return httpx.Response(200, content=self.body, request=request)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")


def _build_worker(transport: httpx.BaseTransport) -> RemoteWorker:
    provider = WishartDirectProvider(base_url="https://wishart.invalid")
    provider._http_client = httpx.Client(
        transport=transport, base_url="https://wishart.invalid"
    )
    return RemoteWorker(
        provider,
        worker_id="worker-a",
        rate_limiter=NoSleepLimiter(),
        circuit=ClosedCircuit(),
        lease_seconds=0.1,
    )


def _seed_ready_task() -> None:
    compound = CompoundStructure.objects.create(
        inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
        canonical_smiles="CCO",
        original_input="CCO",
    )
    RemoteTask.objects.create(
        inchikey=compound.inchikey,
        compound=compound,
        state=TaskState.READY,
        next_run_at=timezone.now(),
    )


def _apply_settings(settings) -> None:
    settings.REMOTE_ENABLED = True
    settings.REMOTE_BATCH_SIZE = 1
    settings.REMOTE_SUBMISSION_RECONCILE_MAX = 1
    settings.REMOTE_SUBMISSION_TTL_SECONDS = 1
    settings.REMOTE_POLL_MIN_SECONDS = 1


@pytest.mark.django_db(transaction=True)
def test_submit_2xx_malformed_body_parks_unknown_without_repost(settings):
    _apply_settings(settings)
    _seed_ready_task()

    # 200 OK but the body is not valid JSON — the client cannot read the query
    # id. The provider very likely created the query; auto re-POST is forbidden.
    transport = Accepted2xxBodyTransport(b"not json{")
    worker = _build_worker(transport)

    assert worker.tick() is True
    assert transport.submit_count == 1
    submission = RemoteSubmission.objects.get()
    assert submission.state == SubmissionState.UNKNOWN

    for _ in range(5):
        assert worker.tick() in {True, False}

    assert transport.submit_count == 1
    submission.refresh_from_db()
    assert submission.state == SubmissionState.UNKNOWN


@pytest.mark.django_db(transaction=True)
def test_submit_2xx_bad_query_id_schema_parks_unknown_without_repost(settings):
    _apply_settings(settings)
    _seed_ready_task()

    # 200 OK with VALID JSON that lacks the query-id field, which
    # validate_query_submit_payload rejects (ProviderContractError). The POST was
    # accepted upstream; this is ambiguous acceptance, not a clean failure.
    transport = Accepted2xxBodyTransport(b'{"status": "accepted"}')
    worker = _build_worker(transport)

    assert worker.tick() is True
    assert transport.submit_count == 1
    submission = RemoteSubmission.objects.get()
    assert submission.state == SubmissionState.UNKNOWN

    for _ in range(5):
        assert worker.tick() in {True, False}

    assert transport.submit_count == 1
    submission.refresh_from_db()
    assert submission.state == SubmissionState.UNKNOWN
