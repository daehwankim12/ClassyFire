"""Task 14 / F-05: the COMPLETE remote provenance contract.

Every entity/submit/poll outcome the worker can produce MUST leave a
``RemoteAttempt`` row that carries the full request/response identity (the
originating task + requested InChIKey, the request payload hash where
applicable, the parsed returned InChIKey where known, and the raw response BODY
text + its sha256 — preserving non-JSON / error bodies verbatim). A result-key
MISMATCH must ADDITIONALLY be stored as an append-only ``ClassificationResult``
event (``REJECTED_MISMATCH``) under the SAME lease-fenced transaction as a
successful result.

The ``remote_outcome_harness`` fixture below drives the REAL ``RemoteWorker``
against a real provider boundary for each named outcome:

* HTTP-shaped outcomes use the REAL ``WishartDirectProvider`` wired to an
  ``httpx.MockTransport`` so wishart.py itself captures the raw body, safe
  headers and status — the attempt fields are populated by production code, not
  the test.
* Transport/timeout outcomes raise from the mock transport so the real
  timeout/transport branches run.

The harness NEVER creates ``RemoteAttempt`` rows directly: every asserted row is
produced by ``RemoteWorker._record_attempt`` through the genuine flow.
"""
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from django.utils import timezone

from apps.batches.models import BatchItem, BatchJob
from apps.classifications.models import ClassificationResult
from apps.compounds.models import CompoundStructure
from apps.core.enums import (
    BatchItemStatus,
    QueryState,
    ResultKind,
    TaskState,
)
from apps.remote import queue
from apps.remote.mock import build_query_response, build_raw
from apps.remote.models import (
    RemoteAttempt,
    RemoteQuery,
    RemoteQueryItem,
    RemoteSubmission,
    RemoteTask,
)
from apps.remote.rate_limit import RateLimiter
from apps.remote.wishart import WishartDirectProvider
from apps.remote.worker import RemoteWorker

REQUIRED_ATTEMPT_FIELDS = [
    "task_id",
    "operation",
    "classification",
    "request_inchikey",
    "raw_body_sha256",
]

IK = "RDHQFKQIGNGIED-MRVPVSSYSA-N"
IK_OTHER = "WQZGKKKJIJFFOK-GASJEMHNSA-N"
SMILES = "CCO"
QUERY_ID = "q-harness-1"


def _entity_response(case: str) -> httpx.Response:
    """The HTTP response the fake Wishart returns for an ENTITY case."""
    if case == "hit":
        return httpx.Response(200, json=build_raw(IK))
    if case == "404_miss":
        return httpx.Response(404, text="entity not found")
    if case == "401":
        return httpx.Response(401, text="unauthorized")
    if case == "403":
        return httpx.Response(403, text="forbidden")
    if case == "400":
        return httpx.Response(400, text="bad request")
    if case == "422":
        return httpx.Response(422, text="unprocessable entity")
    if case == "429":
        return httpx.Response(429, headers={"Retry-After": "5"}, text="slow down")
    if case == "500":
        return httpx.Response(500, text="internal server error")
    if case == "malformed_json":
        return httpx.Response(200, text="<html>not json at all</html>")
    if case == "schema_error":
        # Valid JSON, invalid schema (no usable inchikey) -> contract error.
        return httpx.Response(200, json={"not": "a valid entity payload"})
    raise AssertionError(f"unknown entity case {case!r}")


def _submit_response(case: str) -> httpx.Response:
    if case == "accepted":
        return httpx.Response(200, json={"id": QUERY_ID})
    if case == "401":
        return httpx.Response(401, text="unauthorized")
    if case == "403":
        return httpx.Response(403, text="forbidden")
    if case == "429":
        return httpx.Response(429, headers={"Retry-After": "5"}, text="slow down")
    if case == "500":
        return httpx.Response(500, text="internal server error")
    if case == "non_json_error_body":
        # A retryable status (503: not processed) whose body is NOT JSON: must be
        # preserved verbatim on the attempt and classified RETRYABLE.
        return httpx.Response(503, text="<html>upstream unavailable</html>")
    raise AssertionError(f"unknown submit case {case!r}")


def _poll_response(case: str) -> httpx.Response:
    if case == "running":
        return httpx.Response(200, json={"classification_status": "In progress"})
    if case == "complete":
        return httpx.Response(
            200, json=build_query_response([build_raw(IK)])
        )
    if case == "mismatch_result_key":
        return httpx.Response(
            200, json=build_query_response([build_raw(IK_OTHER)])
        )
    if case == "malformed_result":
        return httpx.Response(200, text="<html>not json</html>")
    if case == "schema_error":
        return httpx.Response(
            200, json={"classification_status": "Done", "entities": "not-a-list"}
        )
    raise AssertionError(f"unknown poll case {case!r}")


def _raising_transport(exc: httpx.HTTPError) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return httpx.MockTransport(handler)


def _response_transport(response: httpx.Response) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    return httpx.MockTransport(handler)


class _RemoteOutcomeHarness:
    """Drive the REAL RemoteWorker to produce a single named outcome.

    ``run(operation, case_name)`` seeds a task (+ batch item, + submitted query
    for POLL), wires a real ``WishartDirectProvider`` to an ``httpx.MockTransport``
    that yields the requested outcome, and ticks the worker. Returns
    ``(task, query, submission)`` — ``query``/``submission`` are ``None`` when the
    operation does not produce one.
    """

    def _provider_with(self, transport: httpx.MockTransport) -> WishartDirectProvider:
        provider = WishartDirectProvider(base_url="https://wishart.test")
        provider._client = lambda: httpx.Client(transport=transport)  # type: ignore[method-assign]
        return provider

    def _worker(self, provider: WishartDirectProvider) -> RemoteWorker:
        return RemoteWorker(
            provider,
            worker_id="w-provenance",
            rate_limiter=RateLimiter(1000, 1000),
        )

    def _seed_task(
        self, *, with_structure: bool = True
    ) -> tuple[RemoteTask, CompoundStructure]:
        job = BatchJob.objects.create(secret_hash="x", total_count=0)
        compound = CompoundStructure.objects.create(
            original_input=SMILES, canonical_smiles=SMILES, inchikey=IK
        )
        BatchItem.objects.create(
            job=job,
            row_order=0,
            original_input=IK,
            inchikey=IK,
            compound=compound,
            status=BatchItemStatus.PENDING_CLASSIFICATION,
        )
        task = queue.enqueue_task(
            IK, compound_id=compound.id if with_structure else None
        )
        assert task is not None
        return task, compound

    def run(
        self, operation: str, case_name: str
    ) -> tuple[RemoteTask, RemoteQuery | None, RemoteSubmission | None]:
        if operation == "ENTITY":
            return self._run_entity(case_name)
        if operation == "SUBMIT":
            return self._run_submit(case_name)
        if operation == "POLL":
            return self._run_poll(case_name)
        raise AssertionError(f"unknown operation {operation!r}")

    def _run_entity(
        self, case_name: str
    ) -> tuple[RemoteTask, None, None]:
        # Seed WITHOUT a structure so a genuine miss (404/MISS) ends at
        # NEED_STRUCTURE and never fans out into a submit attempt — the ENTITY
        # attempt then remains the latest row the test inspects.
        task, _ = self._seed_task(with_structure=False)
        if case_name == "timeout":
            transport = _raising_transport(httpx.ConnectTimeout("timed out"))
        else:
            transport = _response_transport(_entity_response(case_name))
        worker = self._worker(self._provider_with(transport))
        worker.tick()
        return RemoteTask.objects.get(pk=task.pk), None, None

    def _run_submit(
        self, case_name: str
    ) -> tuple[RemoteTask, None, RemoteSubmission | None]:
        # A genuine entity MISS forces the worker into the submit path. The entity
        # GET 404s (miss); the POST /queries then yields the submit outcome. One
        # transport handles BOTH calls by URL.
        task, _ = self._seed_task()
        entity_resp = httpx.Response(404, text="miss")
        # Both "accepted_response_lost" and "timeout" model a POST that may have
        # reached the provider but whose response was lost: the transport raises,
        # the worker parks the intent UNKNOWN (key held).
        submit_raises = case_name in {"accepted_response_lost", "timeout"}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/queries.json"):
                if submit_raises:
                    raise httpx.ConnectError("connection dropped after send")
                return _submit_response(case_name)
            return entity_resp

        worker = self._worker(self._provider_with(httpx.MockTransport(handler)))
        worker.tick()
        submission = RemoteSubmission.objects.order_by("-id").first()
        return RemoteTask.objects.get(pk=task.pk), None, submission

    def _run_poll(
        self, case_name: str
    ) -> tuple[RemoteTask, RemoteQuery, None]:
        # Seed a task already SUBMITTED into a query, then tick to drive the poll.
        task, compound = self._seed_task()
        query = RemoteQuery.objects.create(
            remote_query_id=QUERY_ID,
            payload_hash="hash-poll",
            state=QueryState.SUBMITTED,
        )
        RemoteQueryItem.objects.create(
            query=query, task=task, compound=compound, inchikey=IK
        )
        # Park the task in POLL_WAIT with a due next_run_at so the worker's
        # reactivate_due_tasks flips it READY and claim_task picks it up, routing
        # process() into the poll path.
        queue.transition(
            task,
            TaskState.POLL_WAIT,
            next_run_at=timezone.now() - timedelta(seconds=1),
            lock_owner=None,
            locked_at=None,
        )

        if case_name == "timeout":
            transport = _raising_transport(httpx.ConnectTimeout("timed out"))
        else:
            transport = _response_transport(_poll_response(case_name))
        worker = self._worker(self._provider_with(transport))
        worker.tick()
        return (
            RemoteTask.objects.get(pk=task.pk),
            RemoteQuery.objects.get(pk=query.pk),
            None,
        )


@pytest.fixture
def remote_outcome_harness():
    return _RemoteOutcomeHarness()


@pytest.mark.parametrize(
    ("operation", "case_name", "expected_classification"),
    [
        ("ENTITY", "hit", "FOUND"),
        ("ENTITY", "404_miss", "NOT_FOUND"),
        ("ENTITY", "401", "AUTH_FAILURE"),
        ("ENTITY", "403", "AUTH_FAILURE"),
        # A non-404 4xx (bad key / 400 / 422) is a PERMANENT entity error: it is
        # recorded as such and (Task 17 / F-10) NEVER escalates to a submit —
        # distinct from a malformed/unmapped PROTOCOL_ERROR response below.
        ("ENTITY", "400", "PERMANENT"),
        ("ENTITY", "422", "PERMANENT"),
        ("ENTITY", "429", "RETRYABLE"),
        ("ENTITY", "500", "RETRYABLE"),
        ("ENTITY", "timeout", "RETRYABLE"),
        ("ENTITY", "malformed_json", "PROTOCOL_ERROR"),
        ("ENTITY", "schema_error", "PROTOCOL_ERROR"),
        ("SUBMIT", "accepted", "SUCCESS"),
        ("SUBMIT", "accepted_response_lost", "UNKNOWN_ACCEPTANCE"),
        ("SUBMIT", "401", "AUTH_FAILURE"),
        ("SUBMIT", "403", "AUTH_FAILURE"),
        ("SUBMIT", "429", "RETRYABLE"),
        ("SUBMIT", "500", "UNKNOWN_ACCEPTANCE"),
        ("SUBMIT", "timeout", "UNKNOWN_ACCEPTANCE"),
        ("SUBMIT", "non_json_error_body", "RETRYABLE"),
        ("POLL", "running", "RUNNING"),
        ("POLL", "complete", "COMPLETE"),
        ("POLL", "mismatch_result_key", "RESULT_KEY_MISMATCH"),
        ("POLL", "malformed_result", "PROTOCOL_ERROR"),
        ("POLL", "schema_error", "PROTOCOL_ERROR"),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_every_remote_outcome_records_complete_attempt(
    remote_outcome_harness, operation, case_name, expected_classification
):
    task, query, submission = remote_outcome_harness.run(operation, case_name)
    attempt = RemoteAttempt.objects.latest("id")
    assert attempt.operation == operation
    assert attempt.classification == expected_classification
    assert attempt.task_id == task.id
    assert attempt.request_inchikey == task.inchikey
    assert attempt.raw_body_text is not None
    assert attempt.raw_body_sha256 is not None
    # Timeout/transport paths must record the exception class name so the attempt
    # is auditable (not the literal JSON null/"null" that omitting raw_text yields).
    if case_name == "timeout":
        assert attempt.raw_body_text != "null", (
            f"{operation}/timeout raw_body_text must not be 'null'"
        )
        # The exception class name (e.g. "ConnectTimeout" or "ConnectError") must
        # appear verbatim so the attempt is unambiguously attributable to a
        # transport/timeout failure.
        assert "Connect" in attempt.raw_body_text, (
            f"{operation}/timeout raw_body_text should contain exception class name, "
            f"got {attempt.raw_body_text!r}"
        )
    if operation in {"SUBMIT", "POLL"}:
        assert attempt.request_payload_hash is not None
    if operation == "SUBMIT":
        assert attempt.submission_id == submission.id
        # R-07/F-18 provenance: EVERY submit attempt carries the canonical
        # sha256 of the submitted structure for the representative member.
        assert RemoteAttempt.objects.filter(
            operation=RemoteAttempt.OPERATION_SUBMIT,
            request_structure_hash__regex=r"^[0-9a-f]{64}$",
        ).count() == RemoteAttempt.objects.filter(
            operation=RemoteAttempt.OPERATION_SUBMIT,
        ).count()
    if operation == "POLL":
        assert attempt.query_id == query.id


@pytest.mark.django_db(transaction=True)
def test_mismatch_result_is_append_only_event(remote_outcome_harness):
    task, _query, _submission = remote_outcome_harness.run(
        "POLL", "mismatch_result_key"
    )
    event = ClassificationResult.objects.get(requested_inchikey=task.inchikey)
    assert event.kind == ResultKind.REJECTED_MISMATCH
    assert event.returned_inchikey is not None
    attempt = RemoteAttempt.objects.latest("id")
    assert attempt.returned_inchikey == event.returned_inchikey
