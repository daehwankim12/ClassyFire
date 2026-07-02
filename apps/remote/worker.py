"""The remote worker: claim → process → transition → release.

One worker drains the Postgres queue (``remote_tasks``) using the protocol in
:mod:`apps.remote.queue`. For each claimed task it runs the **entity-first**
flow against the swappable :class:`~apps.remote.provider.Provider`:

1. ``lookup_entity(inchikey)``
   * FOUND  → write live result + fan-out items to FOUND_WISHART_ENTITY + DONE.
   * MISS + structure → submit a /queries batch (up to ``REMOTE_BATCH_SIZE``),
     record a RemoteQuery, park the task in POLL_WAIT.
   * MISS, no structure → fan-out waiting items to NEED_STRUCTURE + DONE.
   * PERMANENT_ERROR → fan-out waiting items to INVALID_INPUT + DONE.
   * RETRYABLE_ERROR → backoff (honour Retry-After) and circuit-record.
2. For SUBMITTED/POLL_WAIT tasks resumed at their poll window, ``poll_query``:
   * RUNNING  → re-park POLL_WAIT.
   * COMPLETE → parse + result-key validate + write + fan-out + DONE.
   * RETRYABLE_ERROR → backoff.

The circuit breaker gates *remote* calls only: when OPEN, the worker parks the
task as DEFERRED and returns — local lookups + queue accumulation keep working
(NFR-P05). When ``REMOTE_ENABLED`` is false the worker never makes remote calls
at all; tasks simply accumulate / stay parked.
"""
from __future__ import annotations

import hashlib
import json
import logging
import socket
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from django.conf import settings
from django.db import DataError, IntegrityError, transaction
from django.utils import timezone

from apps.batches.remote_delay import CIRCUIT_OPEN_MESSAGE
from apps.classifications.result_writer import (
    persistable_returned_inchikey,
    write_live_result,
)
from apps.core.enums import (
    BatchItemStatus,
    ProviderControlState,
    QueryState,
    RemoteQueryItemApplyState,
    SourceType,
    SubmissionState,
    TaskState,
)
from apps.remote import fanout, queue
from apps.remote.circuit import DbCircuitBreaker
from apps.remote.models import (
    RemoteAttempt,
    RemoteProviderControl,
    RemoteQuery,
    RemoteQueryItem,
    RemoteSubmission,
    RemoteSubmissionMember,
    RemoteTask,
)
from apps.remote.provider import (
    AmbiguousSubmissionError,
    AuthProviderError,
    EntityLookupStatus,
    NonRetryableProviderError,
    NormalizedClassification,
    PollStatus,
    ProtocolProviderError,
    Provider,
    RetryableProviderError,
)
from apps.remote.queue import LeaseLost
from apps.remote.rate_limit import DbRateLimiter

logger = logging.getLogger(__name__)


def _bucket_retry_delay_seconds(bucket: object, fallback: float = 5.0) -> float:
    """Return the bucket-specific retry delay after a rate-limit miss."""
    retry_after = getattr(bucket, "retry_after_seconds", None)
    if retry_after is None:
        return fallback
    delay = retry_after()
    if delay <= 0:
        return 0.0
    return float(delay)


RETRY_AFTER_RATE_LIMIT_STATUSES = frozenset({429})


def _should_honor_retry_after(
    *, retry_after: float | None, http_status: int | None
) -> bool:
    return (
        retry_after is not None
        and retry_after > 0
        and http_status in RETRY_AFTER_RATE_LIMIT_STATUSES
    )


def _should_penalize_entity_retry_after(
    *, retry_after: float | None, http_status: int | None
) -> bool:
    return _should_honor_retry_after(
        retry_after=retry_after,
        http_status=http_status,
    )


def _should_escalate_entity_retry_to_submit(
    *,
    has_structure: bool,
    retry_after: float | None,
    http_status: int | None,
    attempt: int = 0,
) -> bool:
    """Whether a retryable entity response should bypass entity backoff."""
    return (
        has_structure
        and http_status is not None
        and http_status >= 500
    )


def _should_terminalize_entity_retry_as_need_structure(
    *,
    has_structure: bool,
    retry_after: float | None,
    http_status: int | None,
    attempt: int = 0,
) -> bool:
    """Whether a retryable entity response is blocked only by missing structure."""
    return (
        not has_structure
        and http_status is not None
        and http_status >= 500
    )


# Provider namespace for the outbox/circuit/rate-bucket rows. Single provider
# today; kept explicit so the idempotency key and shared state are namespaced.
PROVIDER = "wishart"


def _provider_auth_blocked(provider: str = PROVIDER) -> bool:
    """True when an operator-resettable AUTH_BLOCKED gate exists for ``provider``.

    A cheap point read of the single ``remote_provider_controls`` row. Every
    worker's ``tick()`` consults this BEFORE claiming a task so a single durable
    AUTH_BLOCKED state stops ALL worker processes from calling a provider whose
    credentials are bad — until an operator runs ``reset_provider_auth`` (H-03).
    """
    row = RemoteProviderControl.objects.filter(provider=provider).first()
    return row is not None and row.state == ProviderControlState.AUTH_BLOCKED


class _Bucket(Protocol):
    def allow(self) -> bool: ...
    def penalize(self, retry_after: float | None) -> None: ...


class RateLimiterLike(Protocol):
    """Duck-typed limiter: DB-backed (prod) or in-process (tests)."""

    entity: _Bucket
    submit: _Bucket


class CircuitLike(Protocol):
    """Duck-typed breaker: DB-backed (prod) or in-process (tests)."""

    def allow(self) -> bool: ...
    def record_success(self) -> None: ...
    def record_failure(self) -> None: ...

# Default lock lease: how long a claimed task may be held before the recovery
# sweep reclaims it. Derived from the remote timeout with generous headroom.
DEFAULT_LEASE_SECONDS = 300.0
MAX_REMOTE_ATTEMPT_RAW_BODY_CHARS = 65536
TRUNCATED_REMOTE_ATTEMPT_SUFFIX = "\n...[truncated remote response body]"

# Wishart paginates a completed /queries JSON response at 10 compounds/page, and
# our poll (WishartDirectProvider.poll_query) fetches a SINGLE page — so a
# /queries submission must carry no more structures than fit on one page.
# Submitting more silently strands every entity beyond page 1: those tasks see no
# matching entity in the response, look like misses, and loop until they time
# out — never classified. Cap the effective submit batch at the documented page
# size regardless of REMOTE_BATCH_SIZE. (Ref: classyfire.wishartlab.com API docs
# — "JSON files are paginated (10 compounds/page)"; POST limited to 12/min.)
WISHART_QUERY_JSON_PAGE_SIZE = 10


def _effective_batch_size() -> int:
    """Submit batch size clamped to the Wishart JSON page size (see above)."""
    return max(1, min(int(settings.REMOTE_BATCH_SIZE), WISHART_QUERY_JSON_PAGE_SIZE))


def _raw_body_text(raw_payload: object | None, raw_body_text: str | None) -> str:
    """Return the literal response BODY text for hashing/auditing.

    Prefer the verbatim ``raw_body_text`` captured from the provider response
    (preserves non-JSON bodies exactly). When only a parsed payload is available
    (e.g. tests, or a payload reconstructed without the original bytes), fall
    back to canonical JSON so the sha256 is deterministic across processes.
    """
    if raw_body_text is not None:
        return raw_body_text
    return json.dumps(
        raw_payload, sort_keys=True, separators=(",", ":"), default=str
    )


def _stored_raw_body_text(body_text: str) -> str:
    """Return a bounded representation of provider-controlled response text."""
    if len(body_text) <= MAX_REMOTE_ATTEMPT_RAW_BODY_CHARS:
        return body_text
    keep = MAX_REMOTE_ATTEMPT_RAW_BODY_CHARS - len(TRUNCATED_REMOTE_ATTEMPT_SUFFIX)
    return body_text[:keep] + TRUNCATED_REMOTE_ATTEMPT_SUFFIX


def _auditable_inchikey(inchikey: str | None) -> str | None:
    """Only persist full-length InChIKeys in constrained audit columns."""
    if inchikey is None or len(inchikey) != 27:
        return None
    return inchikey


def make_worker_id() -> str:
    """A stable-ish unique id for this worker process (host:pid:uuid)."""
    return f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


class RemoteWorker:
    """Processes remote tasks against an injected provider."""

    def __init__(
        self,
        provider: Provider,
        *,
        worker_id: str | None = None,
        rate_limiter: RateLimiterLike | None = None,
        circuit: CircuitLike | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self.provider = provider
        self.worker_id = worker_id or make_worker_id()
        # Default to the SHARED, DB-backed limiter + circuit (F-08) so N worker
        # processes respect ONE global rate and ONE breaker. Tests may inject the
        # in-process variants for determinism.
        self.rate_limiter = rate_limiter or DbRateLimiter.from_settings()
        self.circuit = circuit or DbCircuitBreaker.from_settings()
        self.lease_seconds = lease_seconds

    # -- top-level steps ---------------------------------------------------

    def tick(self) -> bool:
        """Run one maintenance + claim + process cycle.

        Returns True if a task was processed, False if the queue was idle.
        """
        from apps.operations.admin_views import is_remote_paused

        if is_remote_paused():
            return False
        if _provider_auth_blocked(PROVIDER):
            # Durable provider-wide gate (H-03): a prior 401/403 blocked this
            # provider. Return BEFORE claiming a task so no work is leased only to
            # be deferred — every worker stays idle until an operator resets it.
            return False
        queue.recover_stale_locks(self.lease_seconds)
        queue.reactivate_due_tasks()
        task = queue.claim_task(self.worker_id)
        if task is None:
            return False
        claim = queue.claim_from_task(task)
        try:
            self.process(task, claim=claim)
        except LeaseLost:
            # Our lease was reclaimed mid-flight by another worker (F-07). Abort
            # without overwriting the new owner's state. The reclaim already
            # re-queued the task; we simply drop it.
            logger.warning(
                "lease lost for task %s (inchikey=%s); aborting",
                task.pk,
                task.inchikey,
            )
        return True

    def process(
        self, task: RemoteTask, *, claim: queue.TaskClaim | None = None
    ) -> None:
        """Process a single claimed task to its next state.

        ``claim`` is the IMMUTABLE lease snapshot captured at claim time (F-07).
        When not supplied we derive it FROM THE PASSED ``task`` object's current
        fields — we never re-fetch the row to build it, because the whole point
        is that the snapshot may already be stale relative to the live row.
        """
        claim = claim or queue.claim_from_task(task)
        if not settings.REMOTE_ENABLED:
            # Remote disabled: do NOT make any remote call. Park the task so the
            # queue keeps accumulating without losing work.
            queue.defer_task(task, delay_seconds=self.lease_seconds)
            fanout.mark_items_delayed(task.inchikey)
            return
        if not self.circuit.allow():
            # Circuit open: skip remote calls, park as DEFERRED. Local lookups +
            # queue accumulation continue elsewhere (NFR-P05).
            queue.defer_task(task, delay_seconds=self.lease_seconds)
            fanout.mark_items_delayed(
                task.inchikey,
                message=CIRCUIT_OPEN_MESSAGE,
            )
            return

        try:
            queue.transition(task, TaskState.RUNNING)
        except LeaseLost:
            # Lease stolen before we even started: a fresh worker owns the row.
            # Abort WITHOUT writing anything — do not overwrite the new owner's
            # state. (tick() also guards this; process() is called directly too.)
            logger.warning(
                "lease lost at RUNNING for task %s (inchikey=%s); aborting",
                task.pk,
                task.inchikey,
            )
            return
        # Already-submitted task resumed at its poll window: poll it.
        if task.query_items.filter(query__state__in=[
            QueryState.SUBMITTED, QueryState.POLLING
        ]).exists():
            self._poll_task(task, claim)
        else:
            self._entity_first(task, claim)

    # -- entity-first flow -------------------------------------------------

    def _with_owned_claim(
        self,
        claim: queue.TaskClaim,
        action: Callable[[RemoteTask], None],
    ) -> bool:
        """Run ``action(locked_task)`` ONLY while this worker still owns the lease.

        Row-locks the claim's task and re-asserts the IMMUTABLE lease snapshot
        BEFORE running ``action`` (which performs a branch's DB side effects). If
        the lease was stolen/reclaimed the action never runs and NOTHING is
        written — the stale worker yields to the fresh owner (R-02). Mirrors the
        poll path's lease-fenced writes for the entity-first path. Returns True
        iff the action ran under a valid claim.
        """
        try:
            with transaction.atomic():
                locked = queue.lock_owned_claim(claim)
                action(locked)
            return True
        except LeaseLost:
            logger.warning(
                "lease lost for task %s before entity side effects; aborting",
                claim.task_id,
            )
            return False

    def _entity_first(self, task: RemoteTask, claim: queue.TaskClaim) -> None:
        if not self.rate_limiter.entity.allow():
            queue.defer_task(
                task,
                delay_seconds=_bucket_retry_delay_seconds(self.rate_limiter.entity),
            )
            fanout.mark_items_delayed(task.inchikey)
            return
        try:
            result = self.provider.lookup_entity(task.inchikey)
        except AuthProviderError as exc:
            # A provider that RAISES an auth failure (rather than mapping it to
            # EntityLookupStatus.AUTH_FAILURE) is handled identically: record the
            # attempt, durably block the provider for ALL workers (H-03), and
            # defer this task. Never burns an attempt. Every write is lease-fenced
            # so a stale worker writes nothing (R-02).
            # Bind to a stable local: Python deletes the ``except ... as exc``
            # name at block exit, but the fenced closure references it.
            auth_exc = exc

            def write_auth_raised(locked: RemoteTask) -> None:
                self.circuit.record_failure()
                self._record_attempt(
                    RemoteAttempt.OPERATION_ENTITY,
                    classification="AUTH_FAILURE",
                    raw_payload=None,
                    http_status=auth_exc.http_status,
                    detail=str(auth_exc),
                    task=locked,
                    request_inchikey=locked.inchikey,
                    raw_body_text=auth_exc.raw_text,
                    safe_headers=auth_exc.safe_headers,
                )
                self._block_provider_auth(str(auth_exc))
                queue.defer_task(locked, delay_seconds=self.lease_seconds)

            if self._with_owned_claim(claim, write_auth_raised):
                logger.error(
                    "remote entity AUTH_FAILURE (operator action) for %s: %s",
                    task.inchikey,
                    exc,
                )
                fanout.mark_items_delayed(task.inchikey)
            return

        if result.status == EntityLookupStatus.RETRYABLE_ERROR:
            # A persistent SERVER error (e.g. Wishart 500 for a specific key)
            # does not resolve via repeated entity GETs. If we already have a
            # structure, skip the entity Retry-After/backoff chain and submit a
            # fresh calculation to /queries.json. Rate-limit 429s still honor
            # Retry-After globally, but entity 5xx Retry-After is task-local
            # noise in practice for Wishart's per-key failures. Letting a 500
            # park the shared request bucket strands unrelated keys for 5m.
            structure = self._structure_for(task)
            escalate_to_submit = _should_escalate_entity_retry_to_submit(
                has_structure=structure is not None,
                retry_after=result.retry_after,
                http_status=result.http_status,
                attempt=task.attempt,
            )
            terminalize_need_structure = (
                _should_terminalize_entity_retry_as_need_structure(
                    has_structure=structure is not None,
                    retry_after=result.retry_after,
                    http_status=result.http_status,
                    attempt=task.attempt,
                )
            )

            def write_retryable(locked: RemoteTask) -> None:
                self.circuit.record_failure()
                self._record_attempt(
                    RemoteAttempt.OPERATION_ENTITY,
                    classification="RETRYABLE",
                    raw_payload=result.raw_json,
                    http_status=result.http_status,
                    detail=result.detail,
                    task=locked,
                    request_inchikey=locked.inchikey,
                    raw_body_text=result.raw_text,
                    safe_headers=result.safe_headers,
                )
                if escalate_to_submit:
                    return
                if terminalize_need_structure:
                    fanout.fan_out_result(
                        locked.inchikey, BatchItemStatus.NEED_STRUCTURE
                    )
                    queue.transition_locked(
                        locked, TaskState.DONE, lock_owner=None, locked_at=None
                    )
                    return
                if result.retry_after is not None:
                    if _should_penalize_entity_retry_after(
                        retry_after=result.retry_after,
                        http_status=result.http_status,
                    ):
                        self.rate_limiter.entity.penalize(result.retry_after)
                    queue.retry_after_override(locked, result.retry_after)
                else:
                    queue.schedule_retry(locked)

            owned = self._with_owned_claim(claim, write_retryable)
            if owned and escalate_to_submit:
                # Do NOT re-check the circuit here (the 404→submit path doesn't
                # either): tick() already gated this task, and the 500 recorded
                # above may itself have opened the circuit — a re-check would
                # veto the escalation it just triggered, parking the tripping
                # key in an endless probe → 500 → re-open → defer(300s) loop.
                # The submit outcome drives the breaker: its success closes the
                # circuit, its failure keeps it open.
                logger.warning(
                    "entity lookup returned HTTP %s for %s; "
                    "bypassing entity backoff with a structure submit",
                    result.http_status,
                    task.inchikey,
                )
                self._submit_batch(task, claim)
            elif owned and terminalize_need_structure:
                logger.warning(
                    "entity lookup returned HTTP %s for %s without a submittable "
                    "structure; marking NEED_STRUCTURE instead of retrying",
                    result.http_status,
                    task.inchikey,
                )
            elif owned:
                fanout.mark_items_delayed(task.inchikey)
            return

        if result.status == EntityLookupStatus.AUTH_FAILURE:
            # AUTH_FAILURE: stop retrying, do NOT burn attempts, surface (F-10).
            def write_auth(locked: RemoteTask) -> None:
                self.circuit.record_failure()
                self._record_attempt(
                    RemoteAttempt.OPERATION_ENTITY,
                    classification="AUTH_FAILURE",
                    raw_payload=result.raw_json,
                    http_status=result.http_status,
                    detail=result.detail,
                    task=locked,
                    request_inchikey=locked.inchikey,
                    raw_body_text=result.raw_text,
                    safe_headers=result.safe_headers,
                )
                self._block_provider_auth(result.detail or "entity auth failure")
                queue.defer_task(locked, delay_seconds=self.lease_seconds)

            if self._with_owned_claim(claim, write_auth):
                logger.error(
                    "remote entity AUTH_FAILURE (operator action) for %s: %s",
                    task.inchikey,
                    result.detail,
                )
                fanout.mark_items_delayed(task.inchikey)
            return

        if result.status == EntityLookupStatus.PERMANENT_ERROR:
            # A definite non-404 4xx (bad key / 400 / 422) is a terminal input
            # failure, not provider instability: do not submit a calculation, do
            # not burn retry attempts, and do not trip the shared circuit breaker.
            def write_permanent(locked: RemoteTask) -> None:
                # The provider responded definitively, so a HALF_OPEN probe must
                # complete successfully even though this specific input is invalid.
                self.circuit.record_success()
                self._record_attempt(
                    RemoteAttempt.OPERATION_ENTITY,
                    classification="PERMANENT",
                    raw_payload=result.raw_json,
                    http_status=result.http_status,
                    detail=result.detail,
                    task=locked,
                    request_inchikey=locked.inchikey,
                    raw_body_text=result.raw_text,
                    safe_headers=result.safe_headers,
                )
                fanout.fan_out_result(locked.inchikey, BatchItemStatus.INVALID_INPUT)
                queue.finish_task(locked)

            if self._with_owned_claim(claim, write_permanent):
                logger.warning(
                    "remote entity permanent input error for %s: %s",
                    task.inchikey,
                    result.detail,
                )
            return

        if result.status == EntityLookupStatus.PROTOCOL_ERROR:
            # Unmapped/structurally-broken provider responses are not a cache miss
            # and must never escalate to a structure submit. Unlike definite 4xx
            # input failures above, keep treating these as provider failures that
            # may be resolved by an upstream fix or deploy rollback.
            def write_protocol_error(locked: RemoteTask) -> None:
                self.circuit.record_failure()
                self._record_attempt(
                    RemoteAttempt.OPERATION_ENTITY,
                    classification="PROTOCOL_ERROR",
                    raw_payload=result.raw_json,
                    http_status=result.http_status,
                    detail=result.detail,
                    task=locked,
                    request_inchikey=locked.inchikey,
                    raw_body_text=result.raw_text,
                    safe_headers=result.safe_headers,
                )
                queue.schedule_retry(locked)

            if self._with_owned_claim(claim, write_protocol_error):
                logger.error(
                    "remote entity PROTOCOL_ERROR for %s: %s",
                    task.inchikey,
                    result.detail,
                )
                fanout.mark_items_delayed(task.inchikey)
            return

        if result.status == EntityLookupStatus.FOUND and result.raw_json is not None:
            norm = self.provider.parse_result(result.raw_json)

            def write_found(locked: RemoteTask) -> None:
                self.circuit.record_success()
                self._record_attempt(
                    RemoteAttempt.OPERATION_ENTITY,
                    classification="FOUND",
                    raw_payload=result.raw_json,
                    http_status=result.http_status,
                    task=locked,
                    request_inchikey=locked.inchikey,
                    returned_inchikey=persistable_returned_inchikey(norm.inchikey),
                    raw_body_text=result.raw_text,
                    safe_headers=result.safe_headers,
                )

            if not self._with_owned_claim(claim, write_found):
                return
            if not self._key_matches(task.inchikey, norm.inchikey):
                self._handle_key_mismatch(claim, norm, query=None)
                return
            self._apply_result_with_fence(
                claim,
                query=None,
                norm=norm,
                final_status=BatchItemStatus.FOUND_WISHART_ENTITY,
            )
            return

        # GENUINE entity miss only (HTTP 404 -> NOT_FOUND, or the provider-agnostic
        # MISS from e.g. MockProvider): the key is simply not cached, so we may try
        # to CALCULATE it via a structure submit (else NEED_STRUCTURE). A
        # PROTOCOL_ERROR (400/422) was already handled above and NEVER reaches here,
        # so a malformed request can never trigger a calculation. Be explicit (do
        # not fall through on an unexpected status) so future statuses fail loud.
        if result.status in (EntityLookupStatus.NOT_FOUND, EntityLookupStatus.MISS):
            # Record the entity-miss attempt for a COMPLETE audit trail (F-05):
            # every entity outcome leaves a row, even a genuine cache miss that
            # routes onward to a structure submit. ``str(result.status)`` records
            # NOT_FOUND (HTTP 404) or MISS (provider-agnostic) verbatim. The attempt
            # (and the NEED_STRUCTURE terminal) are lease-fenced; only an owning
            # worker then proceeds to a submit (which re-asserts the claim).
            structure = self._structure_for(task)

            def write_miss(locked: RemoteTask) -> None:
                self.circuit.record_success()
                self._record_attempt(
                    RemoteAttempt.OPERATION_ENTITY,
                    classification=str(result.status),
                    raw_payload=result.raw_json,
                    http_status=result.http_status,
                    detail=result.detail,
                    task=locked,
                    request_inchikey=locked.inchikey,
                    raw_body_text=result.raw_text,
                    safe_headers=result.safe_headers,
                )
                if structure is None:
                    fanout.fan_out_result(
                        locked.inchikey, BatchItemStatus.NEED_STRUCTURE
                    )
                    queue.transition_locked(
                        locked, TaskState.DONE, lock_owner=None, locked_at=None
                    )

            owned = self._with_owned_claim(claim, write_miss)
            if owned and structure is not None:
                self._submit_batch(task, claim)
            return

        # Unexpected/unmapped status with circuit success recorded: surface it as a
        # protocol error rather than silently submitting or dropping the task.
        def write_unexpected(locked: RemoteTask) -> None:
            self.circuit.record_success()
            self._record_attempt(
                RemoteAttempt.OPERATION_ENTITY,
                classification="PROTOCOL_ERROR",
                raw_payload=result.raw_json,
                http_status=result.http_status,
                detail=f"unexpected entity status {result.status}",
                task=locked,
                request_inchikey=locked.inchikey,
                raw_body_text=result.raw_text,
                safe_headers=result.safe_headers,
            )
            queue.schedule_retry(locked)

        if self._with_owned_claim(claim, write_unexpected):
            logger.error(
                "remote entity unexpected status %s for %s; retrying without submit",
                result.status,
                task.inchikey,
            )
            fanout.mark_items_delayed(task.inchikey)

    @staticmethod
    def _canonical_payload(structures: list[tuple[str, str]]) -> dict:
        """Build the canonical (deterministic) request payload for hashing.

        Sorted so the SAME set of structures always hashes identically — the
        idempotency key is stable across retries of the same batch (F-06).
        """
        return {
            "provider": PROVIDER,
            "query_type": "STRUCTURE",
            "structures": sorted([list(s) for s in structures]),
        }

    @staticmethod
    def _payload_hash(payload: dict) -> str:
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _structure_hash(structure: str) -> str:
        """Canonical sha256 of one submitted structure (R-01 reservation key)."""
        return hashlib.sha256(structure.encode("utf-8")).hexdigest()

    @staticmethod
    def _member_specs(
        structures: list[tuple[str, str]]
    ) -> list[dict[str, str]]:
        """Per-member reservation specs parallel to ``structures``."""
        return [
            {
                "inchikey": inchikey,
                "structure": structure,
                "structure_hash": RemoteWorker._structure_hash(structure),
            }
            for inchikey, structure in structures
        ]

    @staticmethod
    def _release_submission_members(
        submission: RemoteSubmission, *, reason: str
    ) -> None:
        """Release every live per-member reservation held by ``submission`` (R-01).

        Called ONLY when the submission deliberately leaves a live state: it FAILS
        (key freed), an operator marks an UNKNOWN intent failed, or a SUBMITTED
        query reaches terminal apply for every member. Never on an automatic
        PENDING→UNKNOWN in-doubt transition (UNKNOWN keeps holding the slot).
        """
        RemoteSubmissionMember.objects.filter(
            submission=submission, active=True
        ).update(
            active=False,
            released_at=timezone.now(),
            release_reason=reason,
        )

    def _filter_reserved_members(
        self,
        members: list[RemoteTask],
        structures: list[tuple[str, str]],
    ) -> tuple[list[RemoteTask], list[tuple[str, str]]]:
        """Drop members already actively reserved by ANOTHER live submission (R-01).

        A member whose ``(inchikey, structure_hash)`` is already reserved is parked
        (DEFERRED) without POSTing — it stays out of every batch until its existing
        submission resolves and releases the reservation. Returns the members and
        their structures that are free to submit.
        """
        inchikeys = [inchikey for inchikey, _structure in structures]
        reserved_pairs = set(
            RemoteSubmissionMember.objects.filter(
                provider=PROVIDER,
                active=True,
                inchikey__in=inchikeys,
            ).values_list("inchikey", "structure_hash")
        )
        free_members: list[RemoteTask] = []
        free_structures: list[tuple[str, str]] = []
        for member, (inchikey, structure) in zip(members, structures, strict=True):
            if (inchikey, self._structure_hash(structure)) in reserved_pairs:
                queue.defer_task(member, delay_seconds=self.lease_seconds)
                fanout.mark_items_delayed(member.inchikey)
            else:
                free_members.append(member)
                free_structures.append((inchikey, structure))
        return free_members, free_structures

    def _submit_batch(self, task: RemoteTask, claim: queue.TaskClaim) -> None:
        # R-02: never INITIATE a submit under a lost lease. A stale entity worker
        # whose lease was stolen during lookup_entity must POST nothing. Re-assert
        # ownership of the representative task before claiming extras or persisting
        # any intent; if the lease is gone the fresh owner drives the submit.
        try:
            with transaction.atomic():
                queue.lock_owned_claim(claim)
        except LeaseLost:
            logger.warning(
                "lease lost before submit for task %s (inchikey=%s); aborting",
                task.pk,
                task.inchikey,
            )
            return
        # Batch this task with other READY tasks that have a structure, up to the
        # effective batch size (clamped to the Wishart JSON page size). The dedup
        # index guarantees one task per inchikey.
        batch = [task]
        if _effective_batch_size() > 1:
            batch.extend(self._claim_batch_extras(task))

        structures: list[tuple[str, str]] = []
        members: list[RemoteTask] = []
        for member in batch:
            struct = self._structure_for(member)
            if struct is None:
                continue
            structures.append((member.inchikey, struct))
            members.append(member)

        payload = self._canonical_payload(structures)
        payload_hash = self._payload_hash(payload)

        # --- F-06: transactional submission outbox + idempotency -------------
        # Reconcile against any existing live (PENDING/SUBMITTED) intent for this
        # exact payload BEFORE drawing a submit token or POSTing (MINOR-1) — a
        # crash between a prior POST and its commit left an authoritative record
        # we must honour, and an in-doubt re-park must NOT burn a global token.
        existing = (
            RemoteSubmission.objects.filter(
                provider=PROVIDER,
                payload_hash=payload_hash,
                state__in=[s.value for s in SubmissionState.live_states()],
            )
            .order_by("-id")
            .first()
        )
        if existing is not None:
            self._reconcile_submission(existing, members)
            return

        # --- R-01: per-member live reservation pre-filter --------------------
        # An exact-payload retry reconciled above. A REGROUPED batch (different
        # payload_hash, e.g. an ambiguous [A,B] later regrouped as [A,C]) reaches
        # here: defer any member still actively reserved by ANOTHER live
        # submission and POST only the free remainder, so the same normalized
        # member is never re-POSTed while its first submission holds the slot.
        members, structures = self._filter_reserved_members(members, structures)
        if not members:
            return
        payload = self._canonical_payload(structures)
        payload_hash = self._payload_hash(payload)
        member_specs = self._member_specs(structures)
        structure_hash_by_key = {
            spec["inchikey"]: spec["structure_hash"] for spec in member_specs
        }
        # The free-member subset may itself match an existing live intent.
        existing = (
            RemoteSubmission.objects.filter(
                provider=PROVIDER,
                payload_hash=payload_hash,
                state__in=[s.value for s in SubmissionState.live_states()],
            )
            .order_by("-id")
            .first()
        )
        if existing is not None:
            self._reconcile_submission(existing, members)
            return
        # The first free member represents the batch for per-attempt audit rows;
        # the original ``task`` may itself have been deferred as reserved above.
        representative = members[0]

        # No existing intent → this is a genuine first/fresh submit. Only NOW
        # draw a global submit token.
        if not self.rate_limiter.submit.allow():
            for member in members:
                queue.defer_task(member, delay_seconds=5)
                fanout.mark_items_delayed(member.inchikey)
            return

        # Persist INTENT (PENDING) + the per-member reservations BEFORE the
        # network call. The partial-UNIQUE on (provider, payload_hash) makes a
        # concurrent/duplicate intent fail fast; the partial-UNIQUE on
        # (provider, inchikey, structure_hash) over active rows makes a concurrent
        # reservation of the same member fail fast too (R-01).
        try:
            with transaction.atomic():
                submission = RemoteSubmission.objects.create(
                    provider=PROVIDER,
                    payload_hash=payload_hash,
                    state=SubmissionState.PENDING,
                    request_payload=payload,
                    # Stamp the TRUE send time immediately before the POST so the
                    # linked query's submitted_at orders by real outbound order
                    # (F-18), not by response-persist order.
                    request_started_at=timezone.now(),
                )
                RemoteSubmissionMember.objects.bulk_create(
                    [
                        RemoteSubmissionMember(
                            submission=submission,
                            provider=PROVIDER,
                            inchikey=spec["inchikey"],
                            structure_hash=spec["structure_hash"],
                        )
                        for spec in member_specs
                    ]
                )
        except IntegrityError:
            # Lost a race on EITHER the payload-hash intent OR a member
            # reservation. If a live intent for this exact payload now exists,
            # reconcile against it; otherwise a concurrent worker reserved one of
            # our members — schedule a retry, on which the pre-filter defers the
            # now-conflicting member and POSTs the remainder.
            existing = (
                RemoteSubmission.objects.filter(
                    provider=PROVIDER,
                    payload_hash=payload_hash,
                    state__in=[s.value for s in SubmissionState.live_states()],
                )
                .order_by("-id")
                .first()
            )
            if existing is not None:
                self._reconcile_submission(existing, members)
            else:
                for member in members:
                    queue.schedule_retry(member)
                    fanout.mark_items_delayed(member.inchikey)
            return

        try:
            submit = self.provider.submit_structures(structures)
        except AmbiguousSubmissionError as exc:
            # The POST may have reached the provider (transport error after the
            # request left): provider acceptance is UNKNOWN. PARK the intent as
            # UNKNOWN — a LIVE state that KEEPS HOLDING the idempotency key — so
            # the worker NEVER auto-resubmits the same (provider, payload_hash)
            # (F-06). MUST NOT call _fail_submission() (which would free the key
            # and allow a duplicate POST). An operator resolves it out-of-band
            # via resolve_submission.
            self.circuit.record_failure()
            detail = (
                "provider acceptance unknown after submit transport error; "
                "automatic re-submit is blocked"
            )
            self._record_attempt(
                RemoteAttempt.OPERATION_SUBMIT,
                classification="UNKNOWN_ACCEPTANCE",
                raw_payload=None,
                detail=f"{detail}: {exc}",
                task=representative,
                submission=submission,
                request_inchikey=representative.inchikey,
                request_payload_hash=payload_hash,
                request_structure_hash=structure_hash_by_key[representative.inchikey],
                http_status=exc.http_status,
                raw_body_text=exc.raw_text,
                safe_headers=exc.safe_headers,
            )
            submission.state = SubmissionState.UNKNOWN
            submission.detail = detail
            submission.save(update_fields=["state", "detail", "updated_at"])
            for member in members:
                queue.defer_task(member, delay_seconds=self.lease_seconds)
                fanout.mark_items_delayed(member.inchikey)
            return
        except RetryableProviderError as exc:
            self.circuit.record_failure()
            self._record_attempt(
                RemoteAttempt.OPERATION_SUBMIT,
                classification="RETRYABLE",
                raw_payload=None,
                detail=str(exc),
                task=representative,
                submission=submission,
                request_inchikey=representative.inchikey,
                request_payload_hash=payload_hash,
                request_structure_hash=structure_hash_by_key[representative.inchikey],
                http_status=exc.http_status,
                raw_body_text=exc.raw_text,
                safe_headers=exc.safe_headers,
            )
            # Free the idempotency key so a later retry can re-submit.
            self._fail_submission(submission, detail=f"retryable: {exc}")
            retry_after = exc.retry_after
            honor_retry_after = _should_honor_retry_after(
                retry_after=retry_after,
                http_status=exc.http_status,
            )
            if honor_retry_after:
                self.rate_limiter.submit.penalize(retry_after)
            for member in members:
                if honor_retry_after:
                    queue.retry_after_override(member, retry_after)
                else:
                    queue.schedule_retry(member)
                fanout.mark_items_delayed(member.inchikey)
            return
        except AuthProviderError as exc:
            # AUTH_FAILURE: stop retrying, do NOT burn attempts, surface (F-10).
            self.circuit.record_failure()
            self._record_attempt(
                RemoteAttempt.OPERATION_SUBMIT,
                classification="AUTH_FAILURE",
                raw_payload=None,
                detail=str(exc),
                task=representative,
                submission=submission,
                request_inchikey=representative.inchikey,
                request_payload_hash=payload_hash,
                request_structure_hash=structure_hash_by_key[representative.inchikey],
                http_status=exc.http_status,
                raw_body_text=exc.raw_text,
                safe_headers=exc.safe_headers,
            )
            self._fail_submission(submission, detail=f"auth: {exc}")
            self._block_provider_auth(str(exc))
            logger.error("remote submit AUTH_FAILURE (operator action): %s", exc)
            for member in members:
                queue.defer_task(member, delay_seconds=self.lease_seconds)
                fanout.mark_items_delayed(member.inchikey)
            return
        except ProtocolProviderError as exc:
            # Unmapped status: never treat as success; surface + defer (F-10).
            self.circuit.record_failure()
            self._record_attempt(
                RemoteAttempt.OPERATION_SUBMIT,
                classification="PROTOCOL_ERROR",
                raw_payload=None,
                detail=str(exc),
                task=representative,
                submission=submission,
                request_inchikey=representative.inchikey,
                request_payload_hash=payload_hash,
                request_structure_hash=structure_hash_by_key[representative.inchikey],
                http_status=exc.http_status,
                raw_body_text=exc.raw_text,
                safe_headers=exc.safe_headers,
            )
            self._fail_submission(submission, detail=f"protocol: {exc}")
            logger.error("remote submit PROTOCOL_ERROR: %s", exc)
            for member in members:
                queue.schedule_retry(member)
                fanout.mark_items_delayed(member.inchikey)
            return
        except NonRetryableProviderError as exc:
            self.circuit.record_success()
            self._record_attempt(
                RemoteAttempt.OPERATION_SUBMIT,
                classification="PERMANENT",
                raw_payload=None,
                detail=str(exc),
                task=representative,
                submission=submission,
                request_inchikey=representative.inchikey,
                request_payload_hash=payload_hash,
                request_structure_hash=structure_hash_by_key[representative.inchikey],
                http_status=exc.http_status,
                raw_body_text=exc.raw_text,
                safe_headers=exc.safe_headers,
            )
            self._fail_submission(submission, detail=f"permanent: {exc}")
            for member in members:
                fanout.fan_out_result(member.inchikey, BatchItemStatus.INVALID_INPUT)
                queue.finish_task(member)
            return

        self.circuit.record_success()
        # --- success: DURABLE-FIRST ordering (IMPORTANT-2) -------------------
        # Step 1: persist the already-POSTed remote_query_id durably — create the
        # RemoteQuery, flip the submission to SUBMITTED, link query items, and
        # record the attempt in ONE transaction. This is fence-free (no
        # lease-conditional writes) so it CANNOT roll back on a member's stolen
        # lease; the remote_query_id is captured even if every member is lost.
        poll_at = timezone.now() + timedelta(
            seconds=settings.REMOTE_POLL_MIN_SECONDS
        )
        try:
            with transaction.atomic():
                query = RemoteQuery.objects.create(
                    remote_query_id=submit.remote_query_id,
                    payload_hash=payload_hash,
                    state=QueryState.SUBMITTED,
                    next_poll_at=poll_at,
                    # True send time (F-18): the moment just before this POST, not now.
                    submitted_at=submission.request_started_at or timezone.now(),
                )
                submission.state = SubmissionState.SUBMITTED
                submission.query = query
                submission.remote_query_id = submit.remote_query_id
                submission.save(
                    update_fields=["state", "query", "remote_query_id", "updated_at"]
                )
                self._record_attempt(
                    RemoteAttempt.OPERATION_SUBMIT,
                    classification="SUCCESS",
                    raw_payload=submit.raw_json,
                    query=query,
                    http_status=submit.http_status,
                    remote_query_id=submit.remote_query_id,
                    task=representative,
                    submission=submission,
                    request_inchikey=representative.inchikey,
                    request_payload_hash=payload_hash,
                    request_structure_hash=structure_hash_by_key[representative.inchikey],
                    raw_body_text=submit.raw_text,
                    safe_headers=submit.safe_headers,
                )
                for member in members:
                    RemoteQueryItem.objects.get_or_create(
                        query=query,
                        task=member,
                        defaults={
                            "compound": member.compound,
                            "inchikey": member.inchikey,
                        },
                    )
        except (IntegrityError, DataError) as exc:
            detail = (
                "provider returned a remote query id that could not be persisted; "
                "automatic re-submit is blocked"
            )
            logger.error("remote submit persistence failure: %s", exc)
            self._record_attempt(
                RemoteAttempt.OPERATION_SUBMIT,
                classification="UNKNOWN_ACCEPTANCE",
                raw_payload=submit.raw_json,
                detail=f"{detail}: {exc}",
                http_status=submit.http_status,
                remote_query_id=submit.remote_query_id,
                task=representative,
                submission=submission,
                request_inchikey=representative.inchikey,
                request_payload_hash=payload_hash,
                request_structure_hash=structure_hash_by_key[representative.inchikey],
                raw_body_text=submit.raw_text,
                safe_headers=submit.safe_headers,
            )
            submission.state = SubmissionState.UNKNOWN
            submission.remote_query_id = submit.remote_query_id
            submission.detail = detail
            submission.save(
                update_fields=["state", "remote_query_id", "detail", "updated_at"]
            )
            for member in members:
                queue.defer_task(member, delay_seconds=self.lease_seconds)
                fanout.mark_items_delayed(member.inchikey)
            return

        # Step 2: park each member in POLL_WAIT, tolerating a per-member
        # LeaseLost. A member whose lease was stolen mid-batch is SKIPPED (left
        # for its new owner / reclaim) rather than aborting the whole batch — the
        # durable query link above means its result is NOT lost: whoever owns the
        # task next finds the RemoteQueryItem and polls the submitted query.
        self._park_members_poll_wait(members, poll_at)

    def _park_members_poll_wait(
        self, members: list[RemoteTask], poll_at: datetime
    ) -> None:
        """POLL_WAIT-park members, skipping any whose lease was lost (F-07)."""
        for member in members:
            try:
                queue.transition(
                    member,
                    TaskState.POLL_WAIT,
                    next_run_at=poll_at,
                    lock_owner=None,
                    locked_at=None,
                )
            except LeaseLost:
                logger.warning(
                    "member %s (inchikey=%s) lease lost during POLL_WAIT park; "
                    "skipping — its query link is durable and it is re-claimable",
                    member.pk,
                    member.inchikey,
                )

    def _reconcile_submission(
        self, submission: RemoteSubmission, members: list[RemoteTask]
    ) -> None:
        """Reconcile a pre-existing submission intent WITHOUT a second POST (F-06).

        * SUBMITTED with a linked query → re-link any unlinked members to that
          query and park them in POLL_WAIT (recover a crash that committed the
          POST result but left tasks unparked). Per-member LeaseLost is tolerated.
        * PENDING (in-doubt: POST may have hit the provider but never committed)
          → do NOT re-POST on this tick. Bump ``reconcile_attempts``; once it
          exceeds the cap OR the intent's TTL has elapsed, PARK it as UNKNOWN.
          UNKNOWN is a LIVE state, so it KEEPS HOLDING the idempotency key — the
          worker NEVER auto-re-POSTs the same structures (which would duplicate
          the upstream submission when the provider had in fact accepted the
          dropped POST). An operator resolves UNKNOWN out-of-band via
          ``manage.py resolve_submission`` (confirm → SUBMITTED, or verify
          non-acceptance → FAILED). The crucial invariant: while the key is held
          (PENDING or UNKNOWN) no second POST is EVER issued.
        """
        poll_at = timezone.now() + timedelta(seconds=settings.REMOTE_POLL_MIN_SECONDS)
        if submission.state == SubmissionState.SUBMITTED and submission.query_id:
            query = submission.query
            with transaction.atomic():
                for member in members:
                    RemoteQueryItem.objects.get_or_create(
                        query=query,
                        task=member,
                        defaults={
                            "compound": member.compound,
                            "inchikey": member.inchikey,
                        },
                    )
            self._park_members_poll_wait(members, poll_at)
            return

        # PENDING in-doubt: NEVER re-POST while the key is held. Once the cap or
        # TTL elapses, PARK as UNKNOWN (still a live state → key STAYS held).
        attempts = submission.reconcile_attempts + 1
        ttl = timedelta(seconds=settings.REMOTE_SUBMISSION_TTL_SECONDS)
        age = timezone.now() - submission.created_at
        park_unknown = (
            attempts > settings.REMOTE_SUBMISSION_RECONCILE_MAX or age >= ttl
        )
        submission.reconcile_attempts = attempts
        if park_unknown:
            submission.state = SubmissionState.UNKNOWN
            submission.detail = (
                f"provider acceptance unknown after {attempts} reconcile(s), "
                f"age={age.total_seconds():.0f}s; automatic re-submit is blocked"
            )
            submission.save(
                update_fields=[
                    "state",
                    "detail",
                    "reconcile_attempts",
                    "updated_at",
                ]
            )
            self._record_attempt(
                RemoteAttempt.OPERATION_SUBMIT,
                classification="UNKNOWN_ACCEPTANCE",
                raw_payload=None,
                remote_query_id=submission.remote_query_id,
                detail=submission.detail,
                submission=submission,
                request_payload_hash=submission.payload_hash,
                # Batch-level reconcile spans many members; record a
                # representative member's key (or null when none) — the brief
                # permits a null request_inchikey here, do NOT fabricate one.
                task=members[0] if members else None,
                request_inchikey=members[0].inchikey if members else None,
            )
            logger.warning(
                "reconcile: in-doubt PENDING submission %s -> UNKNOWN "
                "(key HELD; operator must resolve via resolve_submission)",
                submission.pk,
            )
        else:
            submission.save(update_fields=["reconcile_attempts", "updated_at"])
            logger.warning(
                "reconcile: in-doubt PENDING submission %s (attempt %s/%s); "
                "not re-POSTing yet",
                submission.pk,
                attempts,
                settings.REMOTE_SUBMISSION_RECONCILE_MAX,
            )
        # Re-park members for a later tick regardless. The key stays held in both
        # branches (PENDING below the cap, UNKNOWN at/above it), so no later tick
        # ever re-POSTs — UNKNOWN is cleared only by the operator command.
        for member in members:
            queue.defer_task(member, delay_seconds=self.lease_seconds)
            fanout.mark_items_delayed(member.inchikey)

    @staticmethod
    def _block_provider_auth(detail: str) -> None:
        """Durably block the provider after an auth failure (H-03).

        ``update_or_create`` on the unique ``provider`` column is race-safe: the
        unique constraint serializes concurrent creators (a second creator's
        INSERT fails and is retried as an UPDATE), so N workers hitting the same
        401/403 converge on ONE AUTH_BLOCKED row instead of each deferring only
        their own task and continuing to hammer the provider. The block is
        cleared only by an operator via ``manage.py reset_provider_auth``.
        """
        RemoteProviderControl.objects.update_or_create(
            provider=PROVIDER,
            defaults={
                "state": ProviderControlState.AUTH_BLOCKED,
                "blocked_reason": detail[:2000],
                "credential_version": getattr(
                    settings, "WISHART_CREDENTIAL_VERSION", ""
                ),
            },
        )

    @staticmethod
    def _fail_submission(submission: RemoteSubmission, *, detail: str) -> None:
        """Flip an intent to FAILED so its idempotency key can be reused.

        A FAILED submission no longer holds the idempotency slot, so its per-member
        reservations (R-01) MUST be released too — otherwise a legitimate retry of
        a member that merely hit a retryable submit error could never re-POST.
        """
        with transaction.atomic():
            submission.state = SubmissionState.FAILED
            submission.detail = detail
            submission.save(update_fields=["state", "detail", "updated_at"])
            RemoteWorker._release_submission_members(
                submission, reason="submission_failed"
            )

    def _record_attempt(
        self,
        operation: str,
        *,
        classification: str,
        raw_payload: dict | None,
        query: RemoteQuery | None = None,
        http_status: int | None = None,
        remote_query_id: str | None = None,
        detail: str | None = None,
        task: RemoteTask | None = None,
        submission: RemoteSubmission | None = None,
        request_inchikey: str | None = None,
        returned_inchikey: str | None = None,
        request_payload_hash: str | None = None,
        request_structure_hash: str | None = None,
        raw_body_text: str | None = None,
        safe_headers: dict[str, str] | None = None,
    ) -> None:
        """Append a COMPLETE audit row for ONE remote attempt (F-10).

        Records not just the parsed ``raw_payload`` but the request/response
        IDENTITY: the originating task + its requested InChIKey, the request
        payload/structure hashes, the returned InChIKey where known, the raw
        response BODY text (verbatim, preserving non-JSON bodies) and its
        sha256, and the allow-listed safe response headers. Every remote side
        effect is therefore auditable and reproducible.
        """
        body_text = _raw_body_text(raw_payload, raw_body_text)
        body_sha256 = hashlib.sha256(body_text.encode()).hexdigest()
        stored_body_text = _stored_raw_body_text(body_text)
        RemoteAttempt.objects.create(
            provider=PROVIDER,
            operation=operation,
            classification=classification,
            raw_payload=raw_payload,
            query=query,
            http_status=http_status,
            remote_query_id=remote_query_id,
            detail=detail,
            task=task,
            submission=submission,
            request_inchikey=_auditable_inchikey(request_inchikey),
            returned_inchikey=_auditable_inchikey(returned_inchikey),
            request_payload_hash=request_payload_hash,
            request_structure_hash=request_structure_hash,
            raw_body_text=stored_body_text,
            raw_body_sha256=body_sha256,
            safe_headers=safe_headers,
        )

    def _claim_batch_extras(self, task: RemoteTask) -> list[RemoteTask]:
        """Claim up to REMOTE_BATCH_SIZE-1 extra READY tasks for this batch.

        Each extra is CLAIMED under ``SELECT ... FOR UPDATE SKIP LOCKED`` and
        flipped to LOCKED with this worker's ``lock_owner``/``locked_at`` set —
        mirroring :func:`queue.claim_task` — BEFORE being folded into the submit
        batch. This preserves the single-owner invariant: a concurrent
        ``claim_task`` (or another worker's ``_submit_batch``) cannot also grab a
        row we have already locked, so we never double-submit or mutate a task we
        do not own. Rows we cannot lock are skipped (``skip_locked=True``); rows
        without a submittable structure are released back to READY.
        """
        extras: list[RemoteTask] = []
        exclude_ids = {task.id}
        while len(extras) < _effective_batch_size() - 1:
            extra = queue.claim_task(
                self.worker_id,
                exclude_ids=exclude_ids,
                structure_required=True,
            )
            if extra is None:
                break
            exclude_ids.add(extra.id)
            if extra.query_items.filter(
                query__state__in=[QueryState.SUBMITTED, QueryState.POLLING]
            ).exists():
                queue.transition(
                    extra,
                    TaskState.READY,
                    lock_owner=None,
                    locked_at=None,
                    lease_token=None,
                )
                continue
            if self._structure_for(extra) is None:
                queue.transition(
                    extra,
                    TaskState.READY,
                    lock_owner=None,
                    locked_at=None,
                    lease_token=None,
                )
                continue
            extras.append(extra)
        return extras

    # -- poll flow ---------------------------------------------------------

    def _poll_task(self, task: RemoteTask, claim: queue.TaskClaim) -> None:
        item = (
            task.query_items.filter(
                query__state__in=[QueryState.SUBMITTED, QueryState.POLLING]
            )
            .select_related("query")
            .first()
        )
        if item is None:
            queue.schedule_retry(task)
            fanout.mark_items_delayed(task.inchikey)
            return
        query = item.query
        remote_query_id = query.remote_query_id
        if remote_query_id is None:
            queue.schedule_retry(task)
            fanout.mark_items_delayed(task.inchikey)
            return

        # Polls use the same limiter handle as entity GETs. In production,
        # DbRateLimiter.from_settings aliases entity GETs, polls, and submit
        # POSTs to one upstream "request" bucket because Wishart 429s on
        # aggregate per-IP traffic. Injected test limiters may still model
        # separate entity/submit buckets.
        if not self.rate_limiter.entity.allow():
            queue.transition(
                task,
                TaskState.POLL_WAIT,
                next_run_at=timezone.now()
                + timedelta(
                    seconds=_bucket_retry_delay_seconds(self.rate_limiter.entity)
                ),
                lock_owner=None,
                locked_at=None,
            )
            return

        poll = self.provider.poll_query(remote_query_id)

        # EVERY poll-outcome write (attempt rows + query state + task transition)
        # is fenced behind the IMMUTABLE claim (F-07): we row-lock the task and
        # re-assert the lease against the claim snapshot BEFORE writing anything.
        # A stale worker whose lease was stolen raises LeaseLost here and writes
        # NOTHING — no attempt, no query.state, no transition.
        if poll.status == PollStatus.RETRYABLE_ERROR:
            self.circuit.record_failure()
            try:
                with transaction.atomic():
                    locked = queue.lock_owned_claim(claim)
                    self._record_attempt(
                        RemoteAttempt.OPERATION_POLL,
                        classification="RETRYABLE",
                        raw_payload=poll.raw_json,
                        query=query,
                        http_status=poll.http_status,
                        remote_query_id=remote_query_id,
                        request_payload_hash=query.payload_hash,
                        detail=poll.detail,
                        task=locked,
                        request_inchikey=locked.inchikey,
                        raw_body_text=poll.raw_text,
                        safe_headers=poll.safe_headers,
                    )
                    if poll.retry_after is not None:
                        queue.retry_after_override(locked, poll.retry_after)
                    else:
                        queue.schedule_retry(locked)
            except LeaseLost:
                self._log_poll_lease_lost(task, "RETRYABLE")
                return
            fanout.mark_items_delayed(task.inchikey)
            return

        if poll.status == PollStatus.AUTH_FAILURE:
            # AUTH_FAILURE: stop retrying, do NOT burn attempts, surface (F-10).
            self.circuit.record_failure()
            try:
                with transaction.atomic():
                    locked = queue.lock_owned_claim(claim)
                    self._record_attempt(
                        RemoteAttempt.OPERATION_POLL,
                        classification="AUTH_FAILURE",
                        raw_payload=poll.raw_json,
                        query=query,
                        http_status=poll.http_status,
                        remote_query_id=remote_query_id,
                        request_payload_hash=query.payload_hash,
                        detail=poll.detail,
                        task=locked,
                        request_inchikey=locked.inchikey,
                        raw_body_text=poll.raw_text,
                        safe_headers=poll.safe_headers,
                    )
                    queue.defer_task(locked, delay_seconds=self.lease_seconds)
                    self._block_provider_auth(poll.detail or "poll auth failure")
            except LeaseLost:
                self._log_poll_lease_lost(task, "AUTH_FAILURE")
                return
            logger.error("remote poll AUTH_FAILURE (operator action): %s", poll.detail)
            fanout.mark_items_delayed(task.inchikey)
            return

        if poll.status in (PollStatus.NOT_FOUND, PollStatus.PROTOCOL_ERROR):
            # NOT_FOUND (query absent) or unmapped status: never treat as
            # COMPLETE. Surface + retry rather than silently resolving (F-10).
            #
            # A provider PROTOCOL_ERROR for an already-submitted query means this
            # specific provider query cannot be safely reconciled. Terminally fail
            # the local query/submission and release its per-member reservations
            # before retrying the task; otherwise legacy oversized submissions
            # (or unexpected multipage responses) keep holding R-01 reservations
            # and the capped retry batch is filtered out forever.
            self.circuit.record_failure()
            try:
                with transaction.atomic():
                    locked = queue.lock_owned_claim(claim)
                    self._record_attempt(
                        RemoteAttempt.OPERATION_POLL,
                        classification=str(poll.status),
                        raw_payload=poll.raw_json,
                        query=query,
                        http_status=poll.http_status,
                        remote_query_id=remote_query_id,
                        request_payload_hash=query.payload_hash,
                        detail=poll.detail,
                        task=locked,
                        request_inchikey=locked.inchikey,
                        raw_body_text=poll.raw_text,
                        safe_headers=poll.safe_headers,
                    )
                    if poll.status == PollStatus.PROTOCOL_ERROR:
                        self._fail_query_for_poll_protocol_error(
                            query, detail=poll.detail
                        )
                    queue.schedule_retry(locked)
            except LeaseLost:
                self._log_poll_lease_lost(task, str(poll.status))
                return
            logger.error("remote poll %s: %s", poll.status, poll.detail)
            fanout.mark_items_delayed(task.inchikey)
            return

        self.circuit.record_success()

        if poll.status == PollStatus.RUNNING:
            try:
                with transaction.atomic():
                    locked = queue.lock_owned_claim(claim)
                    self._record_attempt(
                        RemoteAttempt.OPERATION_POLL,
                        classification="RUNNING",
                        raw_payload=poll.raw_json,
                        query=query,
                        http_status=poll.http_status,
                        remote_query_id=remote_query_id,
                        request_payload_hash=query.payload_hash,
                        task=locked,
                        request_inchikey=locked.inchikey,
                        raw_body_text=poll.raw_text,
                        safe_headers=poll.safe_headers,
                    )
                    query.state = QueryState.POLLING
                    query.next_poll_at = timezone.now() + timedelta(
                        seconds=settings.REMOTE_POLL_MIN_SECONDS
                    )
                    query.save(update_fields=["state", "next_poll_at"])
                    queue.transition_locked(
                        locked,
                        TaskState.POLL_WAIT,
                        next_run_at=query.next_poll_at,
                        lock_owner=None,
                        locked_at=None,
                    )
            except LeaseLost:
                self._log_poll_lease_lost(task, "RUNNING")
            return

        # COMPLETE: record the poll attempt + capture the upstream raw_response
        # only if THIS worker still owns the lease, then dispatch each member
        # under its own fence. If the initial claim is lost, abort BEFORE any
        # write so a stale worker never advances the query the fresh owner drives.
        #
        # Task 15: the provider query being Done does NOT by itself make the local
        # query COMPLETE — that is gated on every member reaching a terminal apply
        # state (see _complete_query_if_terminal). A member owned by another worker
        # is parked PENDING_OWNER and the query stays open until its owner applies.
        raw = poll.raw_json or {}
        try:
            with transaction.atomic():
                locked = queue.lock_owned_claim(claim)
                self._record_attempt(
                    RemoteAttempt.OPERATION_POLL,
                    classification="COMPLETE",
                    raw_payload=raw,
                    query=query,
                    http_status=poll.http_status,
                    remote_query_id=remote_query_id,
                    request_payload_hash=query.payload_hash,
                    task=locked,
                    request_inchikey=locked.inchikey,
                    raw_body_text=poll.raw_text,
                    safe_headers=poll.safe_headers,
                )
                query.raw_response = raw
                query.save(update_fields=["raw_response"])
        except LeaseLost:
            self._log_poll_lease_lost(task, "COMPLETE")
            return
        self._apply_query_result(claim, task, query, raw)

    @staticmethod
    def _log_poll_lease_lost(task: RemoteTask, outcome: str) -> None:
        logger.warning(
            "lease lost on poll %s for task %s (inchikey=%s); aborting write",
            outcome,
            task.pk,
            task.inchikey,
        )

    def _apply_query_result(
        self,
        claim: queue.TaskClaim,
        task: RemoteTask,
        query: RemoteQuery,
        raw: dict,
    ) -> None:
        """Resolve EVERY task batched into this completed query.

        A single /queries submission batches up to ``REMOTE_BATCH_SIZE``
        structures; the completed response carries one entity per structure
        (Wishart returns them in submission order). We dispatch each RemoteTask
        linked via ``RemoteQueryItem`` independently — no batched task may be
        left parked after the query completes:

        * an entity is paired with this task and its response key == requested →
          write live result (WISHART_LIVE_QUERY, priority 100) + fan-out + DONE;
        * an entity is paired with this task but its response key != requested →
          RESULT_KEY_MISMATCH + terminal (mirrors the entity-lookup guard);
        * NO entity is available for this task (response omitted it) → treat as
          an unresolved miss and retry per the normal backoff rules.

        Pairing strategy: when the response yields exactly one entity per
        submitted task we pair positionally (submit order == RemoteQueryItem id
        order == ``entities`` order), so a wrong-key slot is caught as a genuine
        mismatch. Otherwise (a partial response) we fall back to matching each
        task to the entity bearing its own InChIKey and retry the absent ones.

        ``task`` (the one we polled with) is resolved on the same footing as its
        batch siblings via the RemoteQueryItem set.
        """
        # Members in deterministic submit order (== the order entities arrive).
        members: list[RemoteTask] = []
        seen_task_ids: set[int] = set()
        for item in query.items.select_related("task").order_by("id"):
            member = item.task
            if member is None or member.id in seen_task_ids:
                continue
            seen_task_ids.add(member.id)
            members.append(member)
        # Defensive: never strand the task we polled with even if its
        # RemoteQueryItem link was severed (SET_NULL).
        if task.id not in seen_task_ids:
            members.append(task)

        max_entities = min(len(members), _effective_batch_size())
        try:
            results = self.provider.parse_results(raw, max_entities=max_entities)
        except ProtocolProviderError as exc:
            logger.error(
                "remote query malformed for %s: %s", query.remote_query_id, exc
            )
            self.circuit.record_failure()
            for member in members:
                # A malformed response is unusable for EVERY member: retry each
                # without a terminal apply state so the query is not completed on
                # a bad response (surplus/garbled entities must not "resolve").
                self._dispatch_member(
                    claim, member, query, None, mark_missing_terminal=False
                )
            self._complete_query_if_terminal(query)
            return

        if len(results) == len(members):
            # Clean one-entity-per-task response: pair positionally and validate
            # each pair's key, so a wrong-key slot surfaces as a mismatch.
            for member, norm in zip(members, results, strict=True):
                self._dispatch_member(claim, member, query, norm)
        else:
            # Partial / ragged response: resolve by InChIKey, retry the absent.
            by_key: dict[str, NormalizedClassification] = {
                norm.inchikey: norm for norm in results if norm.inchikey is not None
            }
            for member in members:
                self._dispatch_member(
                    claim, member, query, by_key.get(member.inchikey)
                )

        # Task 15: only complete the query once EVERY member is in a terminal
        # apply state. A PENDING_OWNER (or still-PENDING/RETRY) member keeps the
        # query open so its owner's later poll/apply finishes it.
        self._complete_query_if_terminal(query)

    def _dispatch_member(
        self,
        claim: queue.TaskClaim,
        member: RemoteTask,
        query: RemoteQuery,
        norm: NormalizedClassification | None,
        *,
        mark_missing_terminal: bool = True,
    ) -> None:
        """Resolve ONE batched member, recording a DURABLE apply outcome (Task 15).

        A member this worker can lock under a valid claim is dispatched and its
        ``RemoteQueryItem`` stamped APPLIED/MISMATCH inside the fenced txn. A
        member actively owned by ANOTHER worker is NOT applied (and never
        silently skipped): its item is parked PENDING_OWNER so the owner's later
        poll/apply completes it. A member missing from an otherwise-COMPLETE
        provider response is marked FAILED before its task retries independently,
        preventing ragged provider responses from keeping the query open. When the
        WHOLE response was rejected as malformed, ``mark_missing_terminal`` is
        False so the member retries WITHOUT a terminal apply state, leaving the
        query open rather than completing it on an unusable response.
        """
        member_claim = self._member_claim(member, claim)
        if member_claim is None:
            # Owned by a different worker (or unlockable): defer to its owner.
            self._park_member_pending_owner(query, member)
            return
        # A member already in a terminal apply state was resolved by a prior poll
        # (possibly by another worker); do not re-apply it.
        if (
            RemoteQueryItem.objects.filter(query=query, task=member)
            .filter(
                apply_state__in=[
                    RemoteQueryItemApplyState.APPLIED,
                    RemoteQueryItemApplyState.MISMATCH,
                    RemoteQueryItemApplyState.FAILED,
                ]
            )
            .exists()
        ):
            return
        self._dispatch_resolved(
            member_claim,
            member,
            query,
            norm,
            mark_missing_terminal=mark_missing_terminal,
        )

    def _park_member_pending_owner(
        self, query: RemoteQuery, member: RemoteTask
    ) -> None:
        owner = member.lock_owner or "no worker"
        RemoteQueryItem.objects.filter(query=query, task=member).exclude(
            apply_state__in=[
                RemoteQueryItemApplyState.APPLIED,
                RemoteQueryItemApplyState.MISMATCH,
                RemoteQueryItemApplyState.FAILED,
            ]
        ).update(
            apply_state=RemoteQueryItemApplyState.PENDING_OWNER,
            apply_detail=f"owned by {owner}; deferred to owner",
        )

    def _complete_query_if_terminal(self, query: RemoteQuery) -> None:
        """Flip the query to COMPLETE only when no member remains non-terminal."""
        terminal_states = [
            RemoteQueryItemApplyState.APPLIED,
            RemoteQueryItemApplyState.MISMATCH,
            RemoteQueryItemApplyState.FAILED,
        ]
        with transaction.atomic():
            locked_query = RemoteQuery.objects.select_for_update().get(pk=query.pk)
            pending_exists = locked_query.items.exclude(
                apply_state__in=terminal_states
            ).exists()
            if pending_exists:
                return
            if (
                locked_query.state == QueryState.COMPLETE
                and locked_query.completed_at is not None
            ):
                return
            locked_query.state = QueryState.COMPLETE
            locked_query.completed_at = timezone.now()
            locked_query.save(update_fields=["state", "completed_at"])
            query.state = locked_query.state
            query.completed_at = locked_query.completed_at
            # Every member reached a terminal apply state, so the submission(s)
            # backing this query no longer need to hold their per-member
            # reservations (R-01): release them in the same transaction that
            # marks the query complete, avoiding stranded active reservations.
            for submission in locked_query.submissions.all():
                self._release_submission_members(submission, reason="query_complete")

    def _fail_query_for_poll_protocol_error(
        self, query: RemoteQuery, *, detail: str | None
    ) -> None:
        """Fail an unreconcilable provider query and free its member slots.

        Poll protocol errors are different from RUNNING/RETRYABLE: the provider
        response for this already-created query cannot be trusted or completely
        consumed. Keep the audit attempt, then mark all still-pending query items
        terminal so the old query no longer owns future retries, fail the linked
        submissions, and release their active per-member reservations.
        """
        terminal_states = [
            RemoteQueryItemApplyState.APPLIED,
            RemoteQueryItemApplyState.MISMATCH,
            RemoteQueryItemApplyState.FAILED,
        ]
        now = timezone.now()
        locked_query = RemoteQuery.objects.select_for_update().get(pk=query.pk)
        RemoteQueryItem.objects.filter(query=locked_query).exclude(
            apply_state__in=terminal_states
        ).update(
            apply_state=RemoteQueryItemApplyState.FAILED,
            apply_detail=(
                "poll protocol error; old provider query failed and retry scheduled"
            ),
            applied_at=now,
        )
        locked_query.state = QueryState.FAILED
        locked_query.completed_at = now
        locked_query.save(update_fields=["state", "completed_at"])
        query.state = locked_query.state
        query.completed_at = locked_query.completed_at
        fail_detail = f"poll protocol error: {detail or 'unreconcilable response'}"
        for submission in locked_query.submissions.select_for_update():
            submission.state = SubmissionState.FAILED
            submission.detail = fail_detail
            submission.save(update_fields=["state", "detail", "updated_at"])
            self._release_submission_members(
                submission, reason="poll_protocol_error"
            )

    def _member_claim(
        self, member: RemoteTask, claim: queue.TaskClaim
    ) -> queue.TaskClaim | None:
        """The fencing claim to use for a batched member (F-07).

        The polled task reuses the IMMUTABLE entry claim. A SIBLING gets a claim
        snapshotted from its OWN current lease (``_claim_for_member_if_owned``);
        :func:`queue.lock_owned_claim` then re-asserts that snapshot at write
        time, so we resolve a sibling only if its lease is unchanged since we read
        it. If a CONCURRENT worker has stolen the sibling's lease between read and
        write, the apply raises LeaseLost and the sibling is SKIPPED for THIS
        task — never corrupted; its durable RemoteQueryItem link means a later
        task (Task 15 makes the skip durable) re-resolves it from the COMPLETE
        query.

        Adaptation vs the brief: the brief gated siblings on
        ``member.lock_owner == self.worker_id``, but in THIS tree batch siblings
        are parked POLL_WAIT with the lock RELEASED (lock_owner=None) before the
        query is polled, so that gate would wrongly strand every legitimate batch
        sibling. Snapshotting the member's actual lease + the lock_owned_claim
        fence is the equivalent of the pre-refactor ``lock_owned_task(member)``
        guard (row-lock + lease self-consistency) and keeps batch resolution
        correct without ever writing an unowned-but-concurrently-stolen sibling.

        Task 15: a sibling ACTIVELY owned by a DIFFERENT worker (its lock_owner
        is set and is not us) is NOT ours to apply — return None so the caller
        parks its RemoteQueryItem PENDING_OWNER, deferring resolution to that
        owner's own poll/apply rather than borrowing its lease. A POLL_WAIT
        sibling (lock_owner released) or a task we already own is applicable.
        """
        if member.pk == claim.task_id:
            return claim
        if member.lock_owner and member.lock_owner != self.worker_id:
            return None
        return self._claim_for_member_if_owned(member)

    def _claim_for_member_if_owned(
        self, member: RemoteTask
    ) -> queue.TaskClaim | None:
        """Snapshot a batch sibling's CURRENT lease into a claim (F-07).

        Returns the claim built from the member's live lease fields. The actual
        ownership decision is deferred to :func:`queue.lock_owned_claim` at write
        time, which raises LeaseLost (→ skip) if a concurrent worker changed the
        lease after we read the member. Returns None only if the member has no pk
        (cannot be locked/fenced).
        """
        if member.pk is None:
            return None
        return queue.claim_from_task(member)

    def _dispatch_resolved(
        self,
        claim: queue.TaskClaim,
        task: RemoteTask,
        query: RemoteQuery,
        norm: NormalizedClassification | None,
        *,
        mark_missing_terminal: bool = True,
    ) -> None:
        if norm is None:
            # No entity for this task's InChIKey: unresolved miss → retry per the
            # normal backoff rules rather than leaving it parked in POLL_WAIT.
            # Fence the retry so a stale worker never re-parks a stolen task.
            try:
                with transaction.atomic():
                    locked = queue.lock_owned_claim(claim)
                    queue.schedule_retry(locked)
                    # Only mark the member terminally FAILED when it is genuinely
                    # absent from an otherwise-COMPLETE response, so the query can
                    # finish. When the WHOLE response was rejected as malformed,
                    # leave the item non-terminal so the query stays open and the
                    # member is reconciled on a later, well-formed response.
                    if mark_missing_terminal:
                        RemoteQueryItem.objects.filter(
                            query=query, task=locked
                        ).exclude(
                            apply_state__in=[
                                RemoteQueryItemApplyState.APPLIED,
                                RemoteQueryItemApplyState.MISMATCH,
                                RemoteQueryItemApplyState.FAILED,
                            ]
                        ).update(
                            apply_state=RemoteQueryItemApplyState.FAILED,
                            apply_detail="missing from completed provider response; retry scheduled",
                            applied_at=timezone.now(),
                        )
            except LeaseLost:
                logger.warning(
                    "lease lost dispatching miss-retry for task %s (inchikey=%s); "
                    "skipping",
                    task.pk,
                    task.inchikey,
                )
                return
            fanout.mark_items_delayed(task.inchikey)
            return
        if not self._key_matches(task.inchikey, norm.inchikey):
            self._handle_key_mismatch(claim, norm, query=query)
            return
        self._apply_result_with_fence(
            claim,
            query=query,
            norm=norm,
            final_status=BatchItemStatus.COMPLETE_AFTER_WISHART,
        )

    # -- fenced result application (F-07) ----------------------------------

    def _apply_result_with_fence(
        self,
        claim: queue.TaskClaim,
        *,
        query: RemoteQuery | None,
        norm: NormalizedClassification,
        final_status: BatchItemStatus,
    ) -> None:
        """Apply a successful result under ONE lease-fenced transaction (F-07).

        The lease guard runs FIRST: :func:`queue.lock_owned_claim` row-locks the
        task and re-asserts this worker still owns the lease — against the
        IMMUTABLE ``claim`` snapshot, never a reloaded row — before ANY side
        effect. A stale worker (whose lease was stolen after a crash/stale-lock
        recovery) raises :class:`LeaseLost` here and writes NOTHING — no
        ClassificationResult event, no fan-out, no transition. Only an owning
        worker reaches the write/fan-out/DONE, and all three commit atomically
        with the lease check, so the result is applied exactly once.
        """
        with transaction.atomic():
            locked = queue.lock_owned_claim(claim)
            write_live_result(
                locked.inchikey,
                norm,
                source_type=(
                    SourceType.WISHART_LIVE_QUERY
                    if query is not None
                    else SourceType.WISHART_LIVE_ENTITY
                ),
                remote_query_id=query.remote_query_id if query is not None else None,
                retrieved_at=query.submitted_at if query is not None else timezone.now(),
            )
            fanout.fan_out_result(locked.inchikey, final_status)
            queue.transition_locked(
                locked, TaskState.DONE, lock_owner=None, locked_at=None
            )
            if query is not None:
                # Task 15: durable per-member outcome, committed with the apply.
                RemoteQueryItem.objects.filter(query=query, task=locked).update(
                    apply_state=RemoteQueryItemApplyState.APPLIED,
                    applied_at=timezone.now(),
                    apply_detail="applied under valid task claim",
                )

    def apply_fenced_result_for_test(
        self, task: RemoteTask, norm: NormalizedClassification, *, remote_query_id: str
    ) -> None:
        """Test-only hook: drive the fenced query-result apply for ``task``.

        Mirrors the query-resolved path: obtains (or creates) the RemoteQuery for
        ``remote_query_id`` exactly as the real poll path links it, then calls
        :meth:`_apply_result_with_fence`. Named ``for_test`` so it is obviously a
        harness entry point and never wired into production flow.
        """
        query, _ = RemoteQuery.objects.get_or_create(
            provider=PROVIDER,
            remote_query_id=remote_query_id,
            defaults={"payload_hash": f"test:{remote_query_id}"},
        )
        self._apply_result_with_fence(
            queue.claim_from_task(task),
            query=query,
            norm=norm,
            final_status=BatchItemStatus.COMPLETE_AFTER_WISHART,
        )

    def create_submission_for_test(self, compound) -> RemoteSubmission:
        """Test-only hook: create a PENDING submission + reservation for ``compound``.

        Mirrors the intent-persist step of ``_submit_batch``: stamps the true
        ``request_started_at`` (send time) and reserves the member, but skips the
        real POST. Lets a test build submissions in a controlled send order and
        then persist their queries in a DIFFERENT order to prove freshness orders
        by true send time (F-18). Named ``for_test`` so it is obviously a harness
        entry point and never wired into production flow.
        """
        structure = (
            compound.canonical_smiles
            or compound.original_smiles
            or compound.inchi
            or compound.inchikey
        )
        structures = [(compound.inchikey, structure)]
        payload = self._canonical_payload(structures)
        payload_hash = self._payload_hash(payload)
        member_specs = self._member_specs(structures)
        with transaction.atomic():
            submission = RemoteSubmission.objects.create(
                provider=PROVIDER,
                payload_hash=payload_hash,
                state=SubmissionState.PENDING,
                request_payload=payload,
                request_started_at=timezone.now(),
            )
            RemoteSubmissionMember.objects.bulk_create(
                [
                    RemoteSubmissionMember(
                        submission=submission,
                        provider=PROVIDER,
                        inchikey=spec["inchikey"],
                        structure_hash=spec["structure_hash"],
                    )
                    for spec in member_specs
                ]
            )
        return submission

    def persist_submit_for_test(
        self, submission: RemoteSubmission, *, remote_query_id: str
    ) -> None:
        """Test-only hook: persist a confirmed submit for ``submission``.

        Mirrors the durable-first success step of ``_submit_batch``: creates the
        RemoteQuery stamping ``submitted_at`` from the submission's TRUE
        ``request_started_at`` (NOT response-persist time) and flips the intent to
        SUBMITTED. Named ``for_test``; never wired into production flow.
        """
        poll_at = timezone.now() + timedelta(seconds=settings.REMOTE_POLL_MIN_SECONDS)
        with transaction.atomic():
            query = RemoteQuery.objects.create(
                provider=PROVIDER,
                remote_query_id=remote_query_id,
                payload_hash=submission.payload_hash,
                state=QueryState.SUBMITTED,
                next_poll_at=poll_at,
                submitted_at=submission.request_started_at or timezone.now(),
            )
            submission.state = SubmissionState.SUBMITTED
            submission.query = query
            submission.remote_query_id = remote_query_id
            submission.save(
                update_fields=["state", "query", "remote_query_id", "updated_at"]
            )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _key_matches(requested: str, returned: str | None) -> bool:
        return returned is not None and returned == requested

    def _handle_key_mismatch(
        self,
        claim: queue.TaskClaim,
        norm: NormalizedClassification,
        *,
        query: RemoteQuery | None,
    ) -> None:
        """Result key mismatch: persist the rejected result, never advance live.

        Routes the mismatched ``norm`` through the SAME lease-fenced apply as a
        clean result. ``write_live_result`` records an immutable
        ``REJECTED_MISMATCH`` event (requested vs returned key + raw payload, with
        remote provenance when ``query`` is set) and deliberately leaves the live
        pointer UNCHANGED, so the mismatch is audited without ever becoming the
        current classification. Fan-out marks waiting items RESULT_KEY_MISMATCH and
        the task ends DONE — all under the F-07 fence, so a STALE worker writes
        NOTHING (the rejected event included).

        A dedicated ``RESULT_KEY_MISMATCH`` RemoteAttempt is recorded inside the
        SAME fenced transaction (carrying the returned key that triggered the
        rejection), so the audit trail names the mismatch outcome explicitly and
        the rejected ClassificationResult event + its attempt commit atomically
        with the lease check.
        """
        with transaction.atomic():
            locked = queue.lock_owned_claim(claim)
            self._record_attempt(
                RemoteAttempt.OPERATION_POLL
                if query is not None
                else RemoteAttempt.OPERATION_ENTITY,
                classification="RESULT_KEY_MISMATCH",
                raw_payload=norm.raw_json,
                query=query,
                remote_query_id=query.remote_query_id if query is not None else None,
                request_payload_hash=query.payload_hash if query is not None else None,
                detail=(
                    f"result key mismatch: requested {locked.inchikey}, "
                    f"returned {norm.inchikey}"
                ),
                task=locked,
                request_inchikey=locked.inchikey,
                returned_inchikey=persistable_returned_inchikey(norm.inchikey),
            )
            outcome = write_live_result(
                locked.inchikey,
                norm,
                source_type=(
                    SourceType.WISHART_LIVE_QUERY
                    if query is not None
                    else SourceType.WISHART_LIVE_ENTITY
                ),
                remote_query_id=query.remote_query_id if query is not None else None,
                retrieved_at=(
                    query.submitted_at if query is not None else timezone.now()
                ),
            )
            # The returned key differs from the requested key, so the writer MUST
            # have appended a REJECTED_MISMATCH event and left the live pointer
            # untouched. Assert the contract rather than trusting it silently.
            assert not outcome.applied, "mismatch must never advance the live pointer"
            fanout.fan_out_result(
                locked.inchikey, BatchItemStatus.RESULT_KEY_MISMATCH
            )
            queue.transition_locked(
                locked, TaskState.DONE, lock_owner=None, locked_at=None
            )
            if query is not None:
                # Task 15: a mismatch is terminal for the member's apply outcome.
                RemoteQueryItem.objects.filter(query=query, task=locked).update(
                    apply_state=RemoteQueryItemApplyState.MISMATCH,
                    applied_at=timezone.now(),
                    apply_detail=(
                        f"result key mismatch: requested {locked.inchikey}, "
                        f"returned {norm.inchikey}"
                    ),
                )

    def _structure_for(self, task: RemoteTask) -> str | None:
        """Return a submittable structure (SMILES/InChI) for the task, if any."""
        compound = task.compound
        if compound is None:
            return None
        return (
            compound.canonical_smiles
            or compound.original_smiles
            or compound.inchi
            or None
        )
