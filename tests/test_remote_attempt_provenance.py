"""Gate 5 / Task 3: complete RemoteAttempt request/response provenance.

Every remote side effect (entity lookup / submit / poll) must leave an audit row
that faithfully records the requested identity (task + InChIKey), the request
payload hash, and the raw response BODY text plus its sha256 — so an attempt is
reproducible even for non-JSON / permanent-error responses.

These tests assert the MODEL faithfully stores the identity threaded through
``RemoteWorker._record_attempt``. The repo has no conftest/factory framework, so
``remote_attempt_factory`` is a thin local fixture that drives the real
``_record_attempt`` with realistic identity for each parametrized outcome.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from apps.batches.models import BatchItem, BatchJob
from apps.classifications.models import ClassificationResult, ClassyfireLive
from apps.compounds.models import CompoundStructure
from apps.core.enums import BatchItemStatus, ResultKind, TaskState
from apps.remote.circuit import CircuitBreaker
from apps.remote.contract import ProviderContractError, validate_entity_lookup_payload
from apps.remote.mock import MockProvider, build_raw
from apps.remote.models import RemoteAttempt, RemoteSubmission, RemoteTask
from apps.remote.rate_limit import RateLimiter
from apps.remote.worker import MAX_REMOTE_ATTEMPT_RAW_BODY_CHARS, RemoteWorker

pytestmark = pytest.mark.django_db(transaction=True)

IK = "RDHQFKQIGNGIED-MRVPVSSYSA-N"
IK_RETURNED = "WQZGKKKJIJFFOK-GASJEMHNSA-N"
SMILES = "CCO"


@pytest.fixture
def remote_attempt_factory():
    """Build a realistic RemoteAttempt via the real ``_record_attempt``.

    Creates a CompoundStructure + RemoteTask (and a RemoteSubmission for submit
    operations), then calls ``RemoteWorker._record_attempt`` with full identity:
    task, 27-char request InChIKey, request payload hash, raw body text, and the
    derived raw_body_sha256. Returns the freshly persisted RemoteAttempt.
    """

    def _make(*, operation: str, classification: str) -> RemoteAttempt:
        worker = RemoteWorker.__new__(RemoteWorker)
        compound = CompoundStructure.objects.create(
            original_input=SMILES, canonical_smiles=SMILES, inchikey=IK
        )
        task = RemoteTask.objects.create(inchikey=IK, compound=compound)
        raw_payload = {"entities": [{"inchikey": IK}], "classification": classification}
        payload = {"provider": "wishart", "structures": [[IK, SMILES]]}
        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        submission = None
        if operation == RemoteAttempt.OPERATION_SUBMIT:
            submission = RemoteSubmission.objects.create(
                provider="wishart",
                payload_hash=payload_hash,
                request_payload=payload,
            )

        worker._record_attempt(
            operation,
            classification=classification,
            raw_payload=raw_payload,
            task=task,
            request_inchikey=task.inchikey,
            request_payload_hash=payload_hash,
            submission=submission,
        )
        return RemoteAttempt.objects.latest("id")

    return _make


@pytest.mark.parametrize(
    "operation,classification",
    [
        ("ENTITY", "FOUND"),
        ("ENTITY", "MISMATCH"),
        ("ENTITY", "AUTH_FAILURE"),
        ("SUBMIT", "SUCCESS"),
        ("SUBMIT", "UNKNOWN_ACCEPTANCE"),
        ("POLL", "COMPLETE"),
        ("POLL", "PROTOCOL_ERROR"),
    ],
)
def test_remote_attempt_carries_request_response_identity(
    remote_attempt_factory,
    operation,
    classification,
):
    attempt = remote_attempt_factory(operation=operation, classification=classification)

    assert attempt.task is not None
    assert attempt.request_inchikey
    assert len(attempt.request_inchikey) == 27
    assert attempt.request_payload_hash
    assert attempt.raw_body_sha256
    assert attempt.raw_body_text is not None


def test_remote_attempt_caps_stored_raw_body_text():
    worker = RemoteWorker.__new__(RemoteWorker)
    compound = CompoundStructure.objects.create(
        original_input=SMILES, canonical_smiles=SMILES, inchikey=IK
    )
    task = RemoteTask.objects.create(inchikey=IK, compound=compound)
    raw_body_text = "x" * (MAX_REMOTE_ATTEMPT_RAW_BODY_CHARS + 1024)

    worker._record_attempt(
        RemoteAttempt.OPERATION_ENTITY,
        classification="RETRYABLE",
        raw_payload=None,
        task=task,
        request_inchikey=task.inchikey,
        raw_body_text=raw_body_text,
    )

    attempt = RemoteAttempt.objects.latest("id")
    assert len(attempt.raw_body_text) == MAX_REMOTE_ATTEMPT_RAW_BODY_CHARS
    assert "truncated remote response body" in attempt.raw_body_text
    assert attempt.raw_body_sha256 == hashlib.sha256(raw_body_text.encode()).hexdigest()


def test_remote_attempt_drops_invalid_returned_inchikey_before_insert():
    worker = RemoteWorker.__new__(RemoteWorker)
    compound = CompoundStructure.objects.create(
        original_input=SMILES, canonical_smiles=SMILES, inchikey=IK
    )
    task = RemoteTask.objects.create(inchikey=IK, compound=compound)

    worker._record_attempt(
        RemoteAttempt.OPERATION_ENTITY,
        classification="RESULT_KEY_MISMATCH",
        raw_payload={"inchikey": "InChIKey=SHORT"},
        task=task,
        request_inchikey=task.inchikey,
        returned_inchikey="SHORT",
    )

    attempt = RemoteAttempt.objects.latest("id")
    assert attempt.returned_inchikey is None


def test_entity_contract_rejects_non_full_length_inchikey():
    with pytest.raises(ProviderContractError):
        validate_entity_lookup_payload(build_raw("SHORT"))


class _MismatchScenario:
    """Drive a real worker entity path that returns a wrong-key classification.

    Builds a CompoundStructure + RemoteTask + BatchItem and a MockProvider whose
    entity lookup for ``requested`` resolves to a payload normalised to a
    DIFFERENT key (``returned``). ``run()`` ticks the worker through the genuine
    entity-lookup mismatch path (no direct ``_handle_key_mismatch`` call), so the
    caller wiring is exercised end-to-end, and returns ``(requested, returned)``.
    """

    requested = IK
    returned = IK_RETURNED

    def run(self, returned: str | None = None) -> tuple[str, str]:
        from apps.remote import queue

        job = BatchJob.objects.create(secret_hash="x", total_count=0)
        compound = CompoundStructure.objects.create(
            original_input=SMILES, canonical_smiles=SMILES, inchikey=self.requested
        )
        BatchItem.objects.create(
            job=job,
            row_order=0,
            original_input=self.requested,
            inchikey=self.requested,
            compound=compound,
            status=BatchItemStatus.PENDING_CLASSIFICATION,
        )
        queue.enqueue_task(self.requested, compound_id=compound.id)

        returned_key = returned or self.returned
        provider = MockProvider(entities={self.requested: build_raw(returned_key)})
        worker = RemoteWorker(
            provider,
            worker_id="w-mismatch",
            rate_limiter=RateLimiter(1000, 1000),
            circuit=CircuitBreaker(failure_threshold=1000),
        )
        worker.tick()
        return self.requested, returned_key


@pytest.fixture
def remote_mismatch_scenario():
    return _MismatchScenario()


def test_remote_key_mismatch_appends_rejected_event(remote_mismatch_scenario):
    requested_key, returned_key = remote_mismatch_scenario.run()

    event = ClassificationResult.objects.get(requested_inchikey=requested_key)
    assert event.returned_inchikey == returned_key
    assert event.kind == ResultKind.REJECTED_MISMATCH
    assert event.raw_json

    # The mismatch must remain TERMINAL but never advance the live pointer.
    task = RemoteTask.objects.get(inchikey=requested_key)
    assert task.state == TaskState.DONE
    assert not ClassyfireLive.objects.filter(inchikey=requested_key).exists()
    assert (
        BatchItem.objects.get(inchikey=requested_key).status
        == BatchItemStatus.RESULT_KEY_MISMATCH
    )


@pytest.mark.parametrize("malformed_returned", ["X", "A" * 128])
def test_remote_key_mismatch_with_malformed_returned_key_is_terminal(
    remote_mismatch_scenario, malformed_returned
):
    requested_key, returned_key = remote_mismatch_scenario.run(malformed_returned)

    event = ClassificationResult.objects.get(requested_inchikey=requested_key)
    assert returned_key == malformed_returned
    assert event.returned_inchikey is None
    assert event.kind == ResultKind.REJECTED_MISMATCH
    assert event.raw_json["inchikey"] == malformed_returned

    attempt = RemoteAttempt.objects.get(classification="RESULT_KEY_MISMATCH")
    assert attempt.request_inchikey == requested_key
    assert attempt.returned_inchikey is None
    assert malformed_returned in (attempt.detail or "")

    task = RemoteTask.objects.get(inchikey=requested_key)
    assert task.state == TaskState.DONE
    assert not ClassyfireLive.objects.filter(inchikey=requested_key).exists()
    assert (
        BatchItem.objects.get(inchikey=requested_key).status
        == BatchItemStatus.RESULT_KEY_MISMATCH
    )
