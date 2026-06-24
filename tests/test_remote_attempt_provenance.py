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

from apps.compounds.models import CompoundStructure
from apps.remote.models import RemoteAttempt, RemoteSubmission, RemoteTask
from apps.remote.worker import RemoteWorker

pytestmark = pytest.mark.django_db(transaction=True)

IK = "RDHQFKQIGNGIED-MRVPVSSYSA-N"
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
