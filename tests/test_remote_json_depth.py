from __future__ import annotations

import json

import httpx
import pytest
from django.utils import timezone

from apps.compounds.models import CompoundStructure
from apps.core.enums import SubmissionState, TaskState
from apps.remote import json_decode, structure_providers
from apps.remote.json_decode import (
    DEFAULT_MAX_JSON_DEPTH,
    JSONNestingTooDeepError,
    loads_bounded_depth,
)
from apps.remote.mock import MockProvider
from apps.remote.models import RemoteAttempt, RemoteSubmission, RemoteTask
from apps.remote.provider import (
    AmbiguousSubmissionError,
    EntityLookupStatus,
    PollStatus,
)
from apps.remote.wishart import WishartDirectProvider
from apps.remote.worker import RemoteWorker

INCHIKEY = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"


def _nested_json(depth: int = DEFAULT_MAX_JSON_DEPTH + 1) -> str:
    return "[" * depth + "0" + "]" * depth


def _nested_object_json(depth: int = DEFAULT_MAX_JSON_DEPTH + 1) -> str:
    return '{"value":' * depth + "0" + "}" * depth


def _wishart_returning(body: str) -> WishartDirectProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, request=request)

    provider = WishartDirectProvider(base_url="https://wishart.test")
    provider._http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return provider


def test_depth_guard_bounds_structure_but_ignores_string_delimiters():
    with pytest.raises(JSONNestingTooDeepError, match="maximum depth"):
        loads_bounded_depth(_nested_json())
    with pytest.raises(JSONNestingTooDeepError, match="maximum depth"):
        loads_bounded_depth(_nested_object_json())

    text = "[\\\"{" * (DEFAULT_MAX_JSON_DEPTH + 1)
    assert loads_bounded_depth(json.dumps({"text": text})) == {"text": text}
    assert loads_bounded_depth(_nested_json(DEFAULT_MAX_JSON_DEPTH)) is not None


def test_depth_guard_normalizes_decoder_recursion_error(monkeypatch):
    def recursive_decoder(_raw_text: str):
        raise RecursionError("decoder recursion")

    monkeypatch.setattr(json_decode.json, "loads", recursive_decoder)

    with pytest.raises(JSONNestingTooDeepError, match="safe decoder depth"):
        loads_bounded_depth("{}")


def test_wishart_entity_rejects_deep_json_as_protocol_error():
    result = _wishart_returning(_nested_json()).lookup_entity(INCHIKEY)

    assert result.status == EntityLookupStatus.PROTOCOL_ERROR
    assert "maximum depth" in (result.detail or "")


def test_wishart_poll_rejects_deep_json_as_protocol_error():
    result = _wishart_returning(_nested_json()).poll_query("query-1")

    assert result.status == PollStatus.PROTOCOL_ERROR
    assert "maximum depth" in (result.detail or "")


def test_wishart_submit_preserves_ambiguity_for_deep_json():
    provider = _wishart_returning(_nested_json())

    with pytest.raises(AmbiguousSubmissionError, match="maximum depth"):
        provider.submit_structures([(INCHIKEY, "CCO")])


def test_pubchem_rejects_deep_json_as_provider_miss(monkeypatch):
    monkeypatch.setattr(
        structure_providers,
        "_request",
        lambda _method, _url: (200, _nested_json()),
    )

    assert structure_providers._pubchem_fetch(INCHIKEY) is None


class _NoSleepLimiter:
    class Bucket:
        def allow(self) -> bool:
            return True

        def penalize(self, retry_after: float | None) -> None:
            pass

    def __init__(self) -> None:
        self.entity = self.Bucket()
        self.submit = self.Bucket()


class _DeepSubmitTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.submit_count = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.startswith("/entities/"):
            return httpx.Response(404, request=request)
        if request.method == "POST" and request.url.path == "/queries.json":
            self.submit_count += 1
            return httpx.Response(200, text=_nested_json(), request=request)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")


@pytest.mark.django_db(transaction=True)
def test_deep_submit_response_parks_unknown_without_repost(settings):
    settings.REMOTE_ENABLED = True
    settings.REMOTE_BATCH_SIZE = 1
    compound = CompoundStructure.objects.create(
        inchikey=INCHIKEY,
        canonical_smiles="CCO",
        original_input="CCO",
    )
    task = RemoteTask.objects.create(
        inchikey=INCHIKEY,
        compound=compound,
        state=TaskState.READY,
        next_run_at=timezone.now(),
    )
    transport = _DeepSubmitTransport()
    provider = WishartDirectProvider(base_url="https://wishart.test")
    provider._http_client = httpx.Client(transport=transport)
    worker = RemoteWorker(
        provider,
        worker_id="deep-submit-worker",
        rate_limiter=_NoSleepLimiter(),
    )

    assert worker.tick() is True

    submission = RemoteSubmission.objects.get()
    assert submission.state == SubmissionState.UNKNOWN
    task.refresh_from_db()
    assert task.state == TaskState.DEFERRED
    assert task.next_run_at is None
    assert worker.tick() is False
    assert transport.submit_count == 1


class _UnexpectedRecursionProvider(MockProvider):
    def lookup_entity(self, inchikey: str):  # type: ignore[override]
        raise RecursionError(f"unexpected recursion for {inchikey}")


@pytest.mark.django_db(transaction=True)
def test_worker_records_and_retries_unexpected_task_recursion(settings):
    settings.REMOTE_ENABLED = True
    compound = CompoundStructure.objects.create(
        inchikey=INCHIKEY,
        canonical_smiles="CCO",
        original_input="CCO",
    )
    task = RemoteTask.objects.create(
        inchikey=INCHIKEY,
        compound=compound,
        state=TaskState.READY,
        next_run_at=timezone.now(),
    )
    worker = RemoteWorker(
        _UnexpectedRecursionProvider(),
        worker_id="depth-guard-worker",
        rate_limiter=_NoSleepLimiter(),
    )

    before = timezone.now()
    assert worker.tick() is True

    task.refresh_from_db()
    # An unexpected entity-path failure is contained and retried on the normal
    # backoff timer, not parked for an operator, so transient faults self-heal.
    assert task.state == TaskState.RETRY_WAIT
    assert task.attempt == 1
    assert task.next_run_at is not None and task.next_run_at > before
    assert task.lock_owner is None
    attempt = RemoteAttempt.objects.get(task=task)
    assert attempt.operation == RemoteAttempt.OPERATION_ENTITY
    assert attempt.classification == "WORKER_ERROR"
    assert "RecursionError" in (attempt.detail or "")
    assert not RemoteSubmission.objects.filter(
        state__in=SubmissionState.live_states()
    ).exists()
