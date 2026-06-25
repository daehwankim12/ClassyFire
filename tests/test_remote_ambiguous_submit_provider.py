from __future__ import annotations

import httpx
import pytest
from django.utils import timezone

from apps.compounds.models import CompoundStructure
from apps.core.enums import SubmissionState, TaskState
from apps.remote.models import RemoteSubmission, RemoteTask
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
        if request.method == "POST" and request.url.path == "/queries":
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
    def allow(self) -> bool:
        return True

    def record_success(self) -> None:
        self.last = "success"

    def record_failure(self) -> None:
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
    RemoteTask.objects.create(
        inchikey=compound.inchikey,
        compound=compound,
        state=TaskState.READY,
        next_run_at=timezone.now(),
    )

    transport = AcceptedThenReadErrorTransport()
    provider = WishartDirectProvider(base_url="https://wishart.invalid")
    provider._http_client = httpx.Client(transport=transport, base_url="https://wishart.invalid")
    worker = RemoteWorker(
        provider,
        worker_id="worker-a",
        rate_limiter=NoSleepLimiter(),
        circuit=ClosedCircuit(),
        lease_seconds=0.1,
    )

    assert worker.tick() is True
    assert transport.submit_count == 1
    submission = RemoteSubmission.objects.get()
    assert submission.state == SubmissionState.UNKNOWN

    for _ in range(5):
        assert worker.tick() in {True, False}

    assert transport.submit_count == 1
    submission.refresh_from_db()
    assert submission.state == SubmissionState.UNKNOWN
