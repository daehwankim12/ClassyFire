"""Gate 5 / Task 1: in-doubt submissions are PARKED, never auto-re-POSTed.

The remote worker submits batches of structures to the Wishart ``/queries`` API.
A ``RemoteSubmission`` outbox row (partial-UNIQUE on ``(provider, payload_hash)``
over the live states) makes the POST idempotent. When the provider may have
ACCEPTED the POST but the socket dropped before the local commit, the intent is
"in-doubt": we must NOT re-POST the same structures (that would duplicate the
Wishart submission). Instead the intent is moved to ``UNKNOWN`` — which still
HOLDS the idempotency key — and its tasks are parked for operator/provider
reconciliation. An operator command (``resolve_submission``) resolves it.

These tests pin the exactly-once invariant: ``submit_structures`` is called
EXACTLY ONCE across many ticks, the submission ends ``UNKNOWN``, and the task is
parked (DEFERRED/RETRY_WAIT) rather than driven to a second POST.
"""
from __future__ import annotations

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.core.enums import QueryState, SubmissionState, TaskState
from apps.remote.models import RemoteQuery, RemoteSubmission, RemoteTask
from apps.remote.provider import EntityLookupResult, EntityLookupStatus
from apps.remote.rate_limit import RateLimiter
from apps.remote.worker import RemoteWorker

pytestmark = pytest.mark.django_db(transaction=True)

IK = "RDHQFKQIGNGIED-MRVPVSSYSA-N"


def _compound(inchikey: str = IK, smiles: str = "CCO"):
    from apps.compounds.models import CompoundStructure

    return CompoundStructure.objects.create(
        original_input=smiles,
        canonical_smiles=smiles,
        inchikey=inchikey,
    )


def _worker(provider) -> RemoteWorker:
    return RemoteWorker(
        provider,
        worker_id="w1",
        rate_limiter=RateLimiter(1000, 1000),
    )


def _ready(inchikey: str = IK) -> None:
    """Re-arm the task as a fresh claimable READY row (mimics stale-lock recovery)."""
    RemoteTask.objects.filter(inchikey=inchikey).update(
        state=TaskState.READY,
        next_run_at=None,
        lock_owner=None,
        locked_at=None,
        lease_token=None,
    )


class AcceptThenDropProvider:
    """Provider that ACCEPTS the POST upstream but drops the socket (in-doubt).

    Entity lookup MISSes so the worker proceeds to a structure submit. The submit
    raises ``ConnectionError`` AFTER incrementing the call counter — simulating a
    request the provider may have accepted before the response was lost.
    """

    def __init__(self) -> None:
        self.submit_count = 0

    def lookup_entity(self, inchikey: str) -> EntityLookupResult:
        return EntityLookupResult(status=EntityLookupStatus.MISS, inchikey=inchikey)

    def submit_structures(self, structures):
        self.submit_count += 1
        raise ConnectionError("socket dropped after provider accepted request")


def test_in_doubt_submission_never_auto_reposts(settings):
    """Across many ticks the in-doubt POST happens ONCE and ends UNKNOWN."""
    settings.REMOTE_SUBMISSION_RECONCILE_MAX = 0
    settings.REMOTE_SUBMISSION_TTL_SECONDS = 0

    comp = _compound()
    task = RemoteTask.objects.create(
        inchikey=comp.inchikey,
        compound=comp,
        state=TaskState.READY,
    )

    provider = AcceptThenDropProvider()
    worker = _worker(provider)

    # Tick 1: entity MISS -> PENDING intent committed -> submit POSTs and the
    # socket drops (ConnectionError is NOT caught; it propagates like any crash
    # between the POST and the local commit, leaving the PENDING intent durable).
    with pytest.raises(ConnectionError):
        worker.tick()
    assert provider.submit_count == 1
    assert RemoteSubmission.objects.get().state == SubmissionState.PENDING

    # Subsequent ticks find the live PENDING intent for the SAME payload and
    # reconcile it WITHOUT a second POST. With cap/TTL at 0 the first reconcile
    # parks the intent as UNKNOWN (idempotency key still held) and NEVER re-POSTs.
    for _ in range(3):
        _ready()
        worker.tick()
        assert provider.submit_count == 1, "must NEVER re-POST an in-doubt submission"

    submission = RemoteSubmission.objects.get()
    assert submission.state == SubmissionState.UNKNOWN
    assert submission.detail and "automatic re-submit is blocked" in submission.detail

    task.refresh_from_db()
    assert task.state in {TaskState.DEFERRED, TaskState.RETRY_WAIT}


def test_unknown_holds_idempotency_key(settings):
    """An UNKNOWN intent keeps the partial-UNIQUE slot: a re-tick still reconciles."""
    settings.REMOTE_SUBMISSION_RECONCILE_MAX = 0
    settings.REMOTE_SUBMISSION_TTL_SECONDS = 0

    comp = _compound()
    RemoteTask.objects.create(
        inchikey=comp.inchikey, compound=comp, state=TaskState.READY
    )
    provider = AcceptThenDropProvider()
    worker = _worker(provider)

    with pytest.raises(ConnectionError):
        worker.tick()
    _ready()
    worker.tick()
    assert RemoteSubmission.objects.get().state == SubmissionState.UNKNOWN

    # Because UNKNOWN is a live state it still matches the idempotency lookup, so
    # a later tick reconciles against it rather than POSTing a duplicate.
    _ready()
    worker.tick()
    assert provider.submit_count == 1
    assert RemoteSubmission.objects.count() == 1
    assert RemoteSubmission.objects.get().state == SubmissionState.UNKNOWN


# ---------------------------------------------------------------------------
# operator command: resolve_submission
# ---------------------------------------------------------------------------


def _unknown_submission(payload_hash: str = "d" * 64) -> RemoteSubmission:
    return RemoteSubmission.objects.create(
        provider="wishart",
        payload_hash=payload_hash,
        state=SubmissionState.UNKNOWN,
        request_payload={"query_type": "STRUCTURE", "structures": []},
        detail="provider acceptance unknown; automatic re-submit is blocked",
    )


def test_resolve_submission_confirm_acceptance_links_query_and_submits():
    sub = _unknown_submission()

    call_command(
        "resolve_submission",
        "--submission-id", str(sub.pk),
        "--remote-query-id", "QID-OPERATOR-CONFIRMED",
    )

    sub.refresh_from_db()
    assert sub.state == SubmissionState.SUBMITTED
    assert sub.remote_query_id == "QID-OPERATOR-CONFIRMED"
    query = RemoteQuery.objects.get(remote_query_id="QID-OPERATOR-CONFIRMED")
    assert sub.query_id == query.pk
    assert query.state == QueryState.SUBMITTED
    assert query.payload_hash == sub.payload_hash


def test_resolve_submission_mark_failed_frees_key():
    sub = _unknown_submission()

    call_command(
        "resolve_submission", "--submission-id", str(sub.pk), "--mark-failed"
    )

    sub.refresh_from_db()
    assert sub.state == SubmissionState.FAILED
    assert RemoteQuery.objects.count() == 0


def test_resolve_submission_refuses_non_unknown_submission():
    sub = RemoteSubmission.objects.create(
        provider="wishart",
        payload_hash="e" * 64,
        state=SubmissionState.PENDING,
        request_payload={"x": 1},
    )
    with pytest.raises(CommandError, match="not UNKNOWN"):
        call_command(
            "resolve_submission", "--submission-id", str(sub.pk), "--mark-failed"
        )
    sub.refresh_from_db()
    assert sub.state == SubmissionState.PENDING  # untouched


def test_resolve_submission_unknown_id_errors():
    with pytest.raises(CommandError, match="No RemoteSubmission"):
        call_command("resolve_submission", "--submission-id", "999999", "--mark-failed")


# ---------------------------------------------------------------------------
# operator command: reconcile_submissions --park-unknown
# ---------------------------------------------------------------------------


def test_reconcile_park_unknown_moves_pending_to_unknown_without_freeing_key():
    """``--park-unknown`` moves a PENDING intent to UNKNOWN; the key stays HELD.

    Parking is the ONLY thing reconcile_submissions does to a live intent — it
    never frees the idempotency key (that is resolve_submission --mark-failed's
    job, and only from UNKNOWN). UNKNOWN is a live state, so a re-park raises a
    CommandError (no PENDING row remains) rather than re-parking or freeing it.
    """
    sub = RemoteSubmission.objects.create(
        provider="wishart",
        payload_hash="f" * 64,
        state=SubmissionState.PENDING,
        request_payload={"query_type": "STRUCTURE", "structures": []},
    )

    call_command("reconcile_submissions", "--park-unknown", str(sub.pk))

    sub.refresh_from_db()
    assert sub.state == SubmissionState.UNKNOWN
    assert sub.detail and "parked" in sub.detail
    # The key is still held: the row is UNKNOWN (a live state), not FAILED.
    assert RemoteSubmission.objects.filter(
        provider="wishart",
        payload_hash="f" * 64,
        state__in=[s.value for s in SubmissionState.live_states()],
    ).exists()


def test_reconcile_park_unknown_refuses_non_pending():
    """``--park-unknown`` only acts on PENDING; a missing PENDING row errors."""
    sub = RemoteSubmission.objects.create(
        provider="wishart",
        payload_hash="a" * 64,
        state=SubmissionState.UNKNOWN,
        request_payload={"x": 1},
    )
    with pytest.raises(CommandError, match="No PENDING submission"):
        call_command("reconcile_submissions", "--park-unknown", str(sub.pk))
    sub.refresh_from_db()
    assert sub.state == SubmissionState.UNKNOWN  # untouched
