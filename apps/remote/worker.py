"""The remote worker: claim → process → transition → release.

One worker drains the Postgres queue (``remote_tasks``) using the protocol in
:mod:`apps.remote.queue`. For each claimed task it runs the **entity-first**
flow against the swappable :class:`~apps.remote.provider.Provider`:

1. ``lookup_entity(inchikey)``
   * FOUND  → write live result + fan-out items to FOUND_WISHART_ENTITY + DONE.
   * MISS + structure → submit a /queries batch (up to ``REMOTE_BATCH_SIZE``),
     record a RemoteQuery, park the task in POLL_WAIT.
   * MISS, no structure → fan-out waiting items to NEED_STRUCTURE + DONE.
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
from datetime import datetime, timedelta
from typing import Protocol

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.classifications.result_writer import write_live_result
from apps.core.enums import (
    BatchItemStatus,
    QueryState,
    SourceType,
    SubmissionState,
    TaskState,
)
from apps.remote import fanout, queue
from apps.remote.circuit import DbCircuitBreaker
from apps.remote.models import (
    RemoteAttempt,
    RemoteQuery,
    RemoteQueryItem,
    RemoteSubmission,
    RemoteTask,
)
from apps.remote.provider import (
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

# Provider namespace for the outbox/circuit/rate-bucket rows. Single provider
# today; kept explicit so the idempotency key and shared state are namespaced.
PROVIDER = "wishart"


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
        queue.recover_stale_locks(self.lease_seconds)
        queue.reactivate_due_tasks()
        task = queue.claim_task(self.worker_id)
        if task is None:
            return False
        try:
            self.process(task)
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

    def process(self, task: RemoteTask) -> None:
        """Process a single claimed task to its next state."""
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
            fanout.mark_items_delayed(task.inchikey)
            return

        queue.transition(task, TaskState.RUNNING)
        # Already-submitted task resumed at its poll window: poll it.
        if task.query_items.filter(query__state__in=[
            QueryState.SUBMITTED, QueryState.POLLING
        ]).exists():
            self._poll_task(task)
        else:
            self._entity_first(task)

    # -- entity-first flow -------------------------------------------------

    def _entity_first(self, task: RemoteTask) -> None:
        if not self.rate_limiter.entity.allow():
            queue.defer_task(task, delay_seconds=5)
            fanout.mark_items_delayed(task.inchikey)
            return
        result = self.provider.lookup_entity(task.inchikey)

        if result.status == EntityLookupStatus.RETRYABLE_ERROR:
            self.circuit.record_failure()
            self._record_attempt(
                RemoteAttempt.OPERATION_ENTITY,
                classification="RETRYABLE",
                raw_payload=result.raw_json,
                http_status=result.http_status,
                detail=result.detail,
                task=task,
                request_inchikey=task.inchikey,
                raw_body_text=result.raw_text,
                safe_headers=result.safe_headers,
            )
            if result.retry_after is not None:
                self.rate_limiter.entity.penalize(result.retry_after)
                queue.retry_after_override(task, result.retry_after)
            else:
                queue.schedule_retry(task)
            fanout.mark_items_delayed(task.inchikey)
            return

        if result.status == EntityLookupStatus.AUTH_FAILURE:
            # AUTH_FAILURE: stop retrying, do NOT burn attempts, surface (F-10).
            self.circuit.record_failure()
            self._record_attempt(
                RemoteAttempt.OPERATION_ENTITY,
                classification="AUTH_FAILURE",
                raw_payload=result.raw_json,
                http_status=result.http_status,
                detail=result.detail,
                task=task,
                request_inchikey=task.inchikey,
                raw_body_text=result.raw_text,
                safe_headers=result.safe_headers,
            )
            logger.error(
                "remote entity AUTH_FAILURE (operator action) for %s: %s",
                task.inchikey,
                result.detail,
            )
            queue.defer_task(task, delay_seconds=self.lease_seconds)
            fanout.mark_items_delayed(task.inchikey)
            return

        if result.status == EntityLookupStatus.PROTOCOL_ERROR:
            # Unmapped status: never treat as MISS/COMPLETE; surface + retry.
            self.circuit.record_failure()
            self._record_attempt(
                RemoteAttempt.OPERATION_ENTITY,
                classification="PROTOCOL_ERROR",
                raw_payload=result.raw_json,
                http_status=result.http_status,
                detail=result.detail,
                task=task,
                request_inchikey=task.inchikey,
                raw_body_text=result.raw_text,
                safe_headers=result.safe_headers,
            )
            logger.error(
                "remote entity PROTOCOL_ERROR for %s: %s",
                task.inchikey,
                result.detail,
            )
            queue.schedule_retry(task)
            fanout.mark_items_delayed(task.inchikey)
            return

        self.circuit.record_success()

        if result.status == EntityLookupStatus.FOUND and result.raw_json is not None:
            norm = self.provider.parse_result(result.raw_json)
            self._record_attempt(
                RemoteAttempt.OPERATION_ENTITY,
                classification="FOUND",
                raw_payload=result.raw_json,
                http_status=result.http_status,
                task=task,
                request_inchikey=task.inchikey,
                returned_inchikey=norm.inchikey,
                raw_body_text=result.raw_text,
                safe_headers=result.safe_headers,
            )
            if not self._key_matches(task.inchikey, norm.inchikey):
                self._handle_key_mismatch(task)
                return
            self._apply_result_with_fence(
                task,
                query=None,
                norm=norm,
                final_status=BatchItemStatus.FOUND_WISHART_ENTITY,
            )
            return

        # MISS: submit a structure query if we have a structure, else NEED_STRUCTURE.
        self._handle_entity_miss(task)

    def _handle_entity_miss(self, task: RemoteTask) -> None:
        structure = self._structure_for(task)
        if structure is None:
            fanout.fan_out_result(task.inchikey, BatchItemStatus.NEED_STRUCTURE)
            queue.finish_task(task)
            return
        self._submit_batch(task)

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

    def _submit_batch(self, task: RemoteTask) -> None:
        # Batch this task with other READY tasks that have a structure, up to
        # REMOTE_BATCH_SIZE. The dedup index guarantees one task per inchikey.
        batch = [task]
        if settings.REMOTE_BATCH_SIZE > 1:
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

        # No existing intent → this is a genuine first/fresh submit. Only NOW
        # draw a global submit token.
        if not self.rate_limiter.submit.allow():
            for member in members:
                queue.defer_task(member, delay_seconds=5)
                fanout.mark_items_delayed(member.inchikey)
            return

        # Persist INTENT (PENDING) BEFORE the network call. The partial-UNIQUE on
        # (provider, payload_hash) makes a concurrent/duplicate intent fail fast.
        try:
            with transaction.atomic():
                submission = RemoteSubmission.objects.create(
                    provider=PROVIDER,
                    payload_hash=payload_hash,
                    state=SubmissionState.PENDING,
                    request_payload=payload,
                )
        except IntegrityError:
            # Another worker raced us to the intent; reconcile against it.
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
        except RetryableProviderError as exc:
            self.circuit.record_failure()
            self._record_attempt(
                RemoteAttempt.OPERATION_SUBMIT,
                classification="RETRYABLE",
                raw_payload=None,
                detail=str(exc),
                task=task,
                submission=submission,
                request_inchikey=task.inchikey,
                request_payload_hash=payload_hash,
                http_status=exc.http_status,
                raw_body_text=exc.raw_text,
                safe_headers=exc.safe_headers,
            )
            # Free the idempotency key so a later retry can re-submit.
            self._fail_submission(submission, detail=f"retryable: {exc}")
            retry_after = exc.retry_after
            if retry_after is not None:
                self.rate_limiter.submit.penalize(retry_after)
            for member in members:
                if retry_after is not None:
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
                task=task,
                submission=submission,
                request_inchikey=task.inchikey,
                request_payload_hash=payload_hash,
                http_status=exc.http_status,
                raw_body_text=exc.raw_text,
                safe_headers=exc.safe_headers,
            )
            self._fail_submission(submission, detail=f"auth: {exc}")
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
                task=task,
                submission=submission,
                request_inchikey=task.inchikey,
                request_payload_hash=payload_hash,
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
                task=task,
                submission=submission,
                request_inchikey=task.inchikey,
                request_payload_hash=payload_hash,
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
        with transaction.atomic():
            query = RemoteQuery.objects.create(
                remote_query_id=submit.remote_query_id,
                payload_hash=payload_hash,
                state=QueryState.SUBMITTED,
                next_poll_at=poll_at,
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
                task=task,
                submission=submission,
                request_inchikey=task.inchikey,
                request_payload_hash=payload_hash,
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
    def _fail_submission(submission: RemoteSubmission, *, detail: str) -> None:
        """Flip an intent to FAILED so its idempotency key can be reused."""
        submission.state = SubmissionState.FAILED
        submission.detail = detail
        submission.save(update_fields=["state", "detail", "updated_at"])

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
            request_inchikey=request_inchikey,
            returned_inchikey=returned_inchikey,
            request_payload_hash=request_payload_hash,
            request_structure_hash=request_structure_hash,
            raw_body_text=body_text,
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
        while len(extras) < settings.REMOTE_BATCH_SIZE - 1:
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

    def _poll_task(self, task: RemoteTask) -> None:
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

        # Deliberate: polls draw from the entity bucket rather than a dedicated
        # poll budget. A poll is a cheap GET against the same host as an entity
        # lookup, and submit (the expensive, separately-budgeted call) already
        # gates how many polls can ever exist — every poll corresponds to an
        # earlier submit. Poll volume is therefore bounded by the submit budget,
        # and a parked poll simply re-parks in POLL_WAIT (no retry counted), so
        # sharing the entity bucket cannot starve fresh entity lookups in
        # practice. A dedicated poll bucket is deferred until poll volume is
        # shown to crowd out entity lookups under real load.
        if not self.rate_limiter.entity.allow():
            queue.transition(
                task,
                TaskState.POLL_WAIT,
                next_run_at=timezone.now() + timedelta(seconds=5),
                lock_owner=None,
                locked_at=None,
            )
            return

        poll = self.provider.poll_query(remote_query_id)

        if poll.status == PollStatus.RETRYABLE_ERROR:
            self.circuit.record_failure()
            self._record_attempt(
                RemoteAttempt.OPERATION_POLL,
                classification="RETRYABLE",
                raw_payload=poll.raw_json,
                query=query,
                http_status=poll.http_status,
                remote_query_id=remote_query_id,
                detail=poll.detail,
                task=task,
                request_inchikey=task.inchikey,
                raw_body_text=poll.raw_text,
                safe_headers=poll.safe_headers,
            )
            if poll.retry_after is not None:
                queue.retry_after_override(task, poll.retry_after)
            else:
                queue.schedule_retry(task)
            fanout.mark_items_delayed(task.inchikey)
            return

        if poll.status == PollStatus.AUTH_FAILURE:
            # AUTH_FAILURE: stop retrying, do NOT burn attempts, surface (F-10).
            self.circuit.record_failure()
            self._record_attempt(
                RemoteAttempt.OPERATION_POLL,
                classification="AUTH_FAILURE",
                raw_payload=poll.raw_json,
                query=query,
                http_status=poll.http_status,
                remote_query_id=remote_query_id,
                detail=poll.detail,
                task=task,
                request_inchikey=task.inchikey,
                raw_body_text=poll.raw_text,
                safe_headers=poll.safe_headers,
            )
            logger.error("remote poll AUTH_FAILURE (operator action): %s", poll.detail)
            queue.defer_task(task, delay_seconds=self.lease_seconds)
            fanout.mark_items_delayed(task.inchikey)
            return

        if poll.status in (PollStatus.NOT_FOUND, PollStatus.PROTOCOL_ERROR):
            # NOT_FOUND (query absent) or unmapped status: never treat as
            # COMPLETE. Surface + retry rather than silently resolving (F-10).
            self.circuit.record_failure()
            self._record_attempt(
                RemoteAttempt.OPERATION_POLL,
                classification=str(poll.status),
                raw_payload=poll.raw_json,
                query=query,
                http_status=poll.http_status,
                remote_query_id=remote_query_id,
                detail=poll.detail,
                task=task,
                request_inchikey=task.inchikey,
                raw_body_text=poll.raw_text,
                safe_headers=poll.safe_headers,
            )
            logger.error("remote poll %s: %s", poll.status, poll.detail)
            queue.schedule_retry(task)
            fanout.mark_items_delayed(task.inchikey)
            return

        self.circuit.record_success()

        if poll.status == PollStatus.RUNNING:
            self._record_attempt(
                RemoteAttempt.OPERATION_POLL,
                classification="RUNNING",
                raw_payload=poll.raw_json,
                query=query,
                http_status=poll.http_status,
                remote_query_id=remote_query_id,
                task=task,
                request_inchikey=task.inchikey,
                raw_body_text=poll.raw_text,
                safe_headers=poll.safe_headers,
            )
            query.state = QueryState.POLLING
            query.next_poll_at = timezone.now() + timedelta(
                seconds=settings.REMOTE_POLL_MIN_SECONDS
            )
            query.save(update_fields=["state", "next_poll_at"])
            queue.transition(
                task,
                TaskState.POLL_WAIT,
                next_run_at=query.next_poll_at,
                lock_owner=None,
                locked_at=None,
            )
            return

        # COMPLETE
        raw = poll.raw_json or {}
        self._record_attempt(
            RemoteAttempt.OPERATION_POLL,
            classification="COMPLETE",
            raw_payload=raw,
            query=query,
            http_status=poll.http_status,
            remote_query_id=remote_query_id,
            task=task,
            request_inchikey=task.inchikey,
            raw_body_text=poll.raw_text,
            safe_headers=poll.safe_headers,
        )
        query.state = QueryState.COMPLETE
        query.raw_response = raw
        query.completed_at = timezone.now()
        query.save(update_fields=["state", "raw_response", "completed_at"])
        self._apply_query_result(task, query, raw)

    def _apply_query_result(
        self, task: RemoteTask, query: RemoteQuery, raw: dict
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
        results = self.provider.parse_results(raw)

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

        if len(results) == len(members):
            # Clean one-entity-per-task response: pair positionally and validate
            # each pair's key, so a wrong-key slot surfaces as a mismatch.
            for member, norm in zip(members, results, strict=True):
                self._dispatch_resolved(member, query, norm)
            return

        # Partial / ragged response: resolve by InChIKey, retry the absent.
        by_key: dict[str, NormalizedClassification] = {
            norm.inchikey: norm for norm in results if norm.inchikey is not None
        }
        for member in members:
            self._dispatch_resolved(member, query, by_key.get(member.inchikey))

    def _dispatch_resolved(
        self,
        task: RemoteTask,
        query: RemoteQuery,
        norm: NormalizedClassification | None,
    ) -> None:
        if norm is None:
            # No entity for this task's InChIKey: unresolved miss → retry per the
            # normal backoff rules rather than leaving it parked in POLL_WAIT.
            queue.schedule_retry(task)
            fanout.mark_items_delayed(task.inchikey)
            return
        if not self._key_matches(task.inchikey, norm.inchikey):
            self._handle_key_mismatch(task)
            return
        self._apply_result_with_fence(
            task,
            query=query,
            norm=norm,
            final_status=BatchItemStatus.COMPLETE_AFTER_WISHART,
        )

    # -- fenced result application (F-07) ----------------------------------

    def _apply_result_with_fence(
        self,
        task: RemoteTask,
        *,
        query: RemoteQuery | None,
        norm: NormalizedClassification,
        final_status: BatchItemStatus,
    ) -> None:
        """Apply a successful result under ONE lease-fenced transaction (F-07).

        The lease guard runs FIRST: :func:`queue.lock_owned_task` row-locks the
        task and re-asserts this worker still owns the lease before ANY side
        effect. A stale worker (whose lease was stolen after a crash/stale-lock
        recovery) raises :class:`LeaseLost` here and writes NOTHING — no
        ClassificationResult event, no fan-out, no transition. Only an owning
        worker reaches the write/fan-out/DONE, and all three commit atomically
        with the lease check, so the result is applied exactly once.
        """
        with transaction.atomic():
            locked = queue.lock_owned_task(task)
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
            task,
            query=query,
            norm=norm,
            final_status=BatchItemStatus.COMPLETE_AFTER_WISHART,
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _key_matches(requested: str, returned: str | None) -> bool:
        return returned is not None and returned == requested

    def _handle_key_mismatch(self, task: RemoteTask) -> None:
        """Result key mismatch: do NOT auto-apply; mark items + terminal task."""
        fanout.fan_out_result(task.inchikey, BatchItemStatus.RESULT_KEY_MISMATCH)
        queue.finish_task(task)

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
