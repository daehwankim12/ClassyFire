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
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.classifications.result_writer import write_live_result
from apps.core.enums import (
    BatchItemStatus,
    QueryState,
    SourceType,
    TaskState,
)
from apps.remote import fanout, queue
from apps.remote.circuit import CircuitBreaker
from apps.remote.models import RemoteQuery, RemoteQueryItem, RemoteTask
from apps.remote.provider import (
    EntityLookupStatus,
    NonRetryableProviderError,
    NormalizedClassification,
    PollStatus,
    Provider,
    RetryableProviderError,
)
from apps.remote.rate_limit import RateLimiter

logger = logging.getLogger(__name__)

# Default lock lease: how long a claimed task may be held before the recovery
# sweep reclaims it. Derived from the remote timeout with generous headroom.
DEFAULT_LEASE_SECONDS = 300.0


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
        rate_limiter: RateLimiter | None = None,
        circuit: CircuitBreaker | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self.provider = provider
        self.worker_id = worker_id or make_worker_id()
        self.rate_limiter = rate_limiter or RateLimiter.from_settings()
        self.circuit = circuit or CircuitBreaker.from_settings()
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
        self.process(task)
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
            if result.retry_after is not None:
                self.rate_limiter.entity.penalize(result.retry_after)
                queue.retry_after_override(task, result.retry_after)
            else:
                queue.schedule_retry(task)
            fanout.mark_items_delayed(task.inchikey)
            return

        self.circuit.record_success()

        if result.status == EntityLookupStatus.FOUND and result.raw_json is not None:
            norm = self.provider.parse_result(result.raw_json)
            if not self._key_matches(task.inchikey, norm.inchikey):
                self._handle_key_mismatch(task)
                return
            write_live_result(
                task.inchikey,
                norm,
                source_type=SourceType.WISHART_LIVE_ENTITY,
                retrieved_at=timezone.now(),
            )
            fanout.fan_out_result(
                task.inchikey, BatchItemStatus.FOUND_WISHART_ENTITY
            )
            queue.finish_task(task)
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

    def _submit_batch(self, task: RemoteTask) -> None:
        if not self.rate_limiter.submit.allow():
            queue.defer_task(task, delay_seconds=5)
            fanout.mark_items_delayed(task.inchikey)
            return

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

        try:
            submit = self.provider.submit_structures(structures)
        except RetryableProviderError as exc:
            self.circuit.record_failure()
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
        except NonRetryableProviderError:
            self.circuit.record_success()
            for member in members:
                fanout.fan_out_result(member.inchikey, BatchItemStatus.INVALID_INPUT)
                queue.finish_task(member)
            return

        self.circuit.record_success()
        payload_hash = hashlib.sha256(
            json.dumps(sorted(structures)).encode()
        ).hexdigest()
        query = RemoteQuery.objects.create(
            remote_query_id=submit.remote_query_id,
            payload_hash=payload_hash,
            state=QueryState.SUBMITTED,
            next_poll_at=timezone.now()
            + timedelta(seconds=settings.REMOTE_POLL_MIN_SECONDS),
        )
        poll_at = timezone.now() + timedelta(
            seconds=settings.REMOTE_POLL_MIN_SECONDS
        )
        for member in members:
            RemoteQueryItem.objects.create(
                query=query,
                task=member,
                compound=member.compound,
                inchikey=member.inchikey,
            )
            queue.transition(
                member,
                TaskState.POLL_WAIT,
                next_run_at=poll_at,
                lock_owner=None,
                locked_at=None,
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
        now = timezone.now()
        extras: list[RemoteTask] = []
        with transaction.atomic():
            candidates = list(
                RemoteTask.objects.select_for_update(skip_locked=True)
                .filter(state=TaskState.READY)
                .filter(Q(next_run_at__isnull=True) | Q(next_run_at__lte=now))
                .exclude(id=task.id)
                .exclude(query_items__query__state__in=[
                    QueryState.SUBMITTED,
                    QueryState.POLLING,
                ])
                .order_by("-priority", "id")[: settings.REMOTE_BATCH_SIZE - 1]
            )
            for extra in candidates:
                if self._structure_for(extra) is None:
                    continue
                queue.transition(
                    extra,
                    TaskState.LOCKED,
                    lock_owner=self.worker_id,
                    locked_at=now,
                )
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
            if poll.retry_after is not None:
                queue.retry_after_override(task, poll.retry_after)
            else:
                queue.schedule_retry(task)
            fanout.mark_items_delayed(task.inchikey)
            return

        self.circuit.record_success()

        if poll.status == PollStatus.RUNNING:
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
        write_live_result(
            task.inchikey,
            norm,
            source_type=SourceType.WISHART_LIVE_QUERY,
            remote_query_id=query.remote_query_id,
            retrieved_at=timezone.now(),
        )
        fanout.fan_out_result(
            task.inchikey, BatchItemStatus.COMPLETE_AFTER_WISHART
        )
        queue.finish_task(task)

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
