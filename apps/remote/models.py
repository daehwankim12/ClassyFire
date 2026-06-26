"""Remote ClassyFire work queue: tasks, queries, and their mapping.

``RemoteTask`` is the language-agnostic queue. Workers in any language claim
work with ``FOR UPDATE SKIP LOCKED`` over this table using the
(state, priority, next_run_at, lock_owner, locked_at, attempt) columns; see
docs/state-machines.md. The partial-unique index on ``inchikey`` over the
active states is the global dedupe of outstanding work.
"""
from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.enums import (
    ACTIVE_TASK_STATES,
    LIVE_SUBMISSION_STATES,
    CircuitState,
    ProviderControlState,
    QueryState,
    RemoteQueryItemApplyState,
    SubmissionState,
    TaskState,
)

INCHIKEY_REGEX = r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$"


class RemoteTask(models.Model):
    """A unit of outstanding remote classification work for one InChIKey."""

    inchikey = models.CharField(max_length=27, help_text="Target InChIKey.")
    compound = models.ForeignKey(
        "compounds.CompoundStructure",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="remote_tasks",
    )

    # --- queue protocol columns (language-agnostic) ---
    state = models.CharField(
        max_length=16, choices=TaskState.choices, default=TaskState.READY
    )
    priority = models.IntegerField(
        default=0, help_text="Higher is claimed first."
    )
    principal = models.CharField(
        max_length=128,
        default="system",
        db_default="system",
        help_text="Principal used for fair scheduling and remote budget accounting.",
    )
    attempt = models.IntegerField(default=0, help_text="Retry attempt counter.")
    next_run_at = models.DateTimeField(
        null=True, blank=True, help_text="Earliest time the task may be claimed."
    )
    lock_owner = models.TextField(
        null=True, blank=True, help_text="Worker id currently holding the task."
    )
    locked_at = models.DateTimeField(
        null=True, blank=True, help_text="When the current lock was taken."
    )

    # --- lease fencing (F-07) ---
    # A fencing token (uuid) + monotonic version issued on every claim/reclaim.
    # EVERY subsequent state transition is conditional on BOTH so a stale worker
    # whose lease was reclaimed cannot overwrite the new owner's state. The
    # version is bumped (never reset) on each claim and on stale-lock recovery.
    lease_token = models.UUIDField(
        null=True,
        blank=True,
        help_text="Fencing token of the worker currently holding the lease.",
    )
    lease_version = models.IntegerField(
        default=0,
        help_text="Monotonic fencing version, bumped on every claim/reclaim.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "remote_tasks"
        constraints = [
            models.CheckConstraint(
                name="remote_task_inchikey_format",
                condition=Q(inchikey__regex=INCHIKEY_REGEX),
            ),
            # Only ONE active task per InChIKey: a partial unique index over the
            # active states (everything except DONE). DONE rows do not conflict.
            models.UniqueConstraint(
                fields=["inchikey"],
                name="remote_task_active_inchikey_unique",
                condition=Q(state__in=ACTIVE_TASK_STATES),
            ),
        ]
        indexes = [
            # Supports the FOR UPDATE SKIP LOCKED claim query.
            models.Index(
                fields=["state", "priority", "next_run_at"],
                name="remote_task_claim_idx",
            ),
            models.Index(
                fields=["principal", "state"],
                name="remote_task_princ_state_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"task:{self.inchikey} ({self.state})"


class RemoteQuery(models.Model):
    """A single submitted ClassyFire query/batch and its polling lifecycle."""

    provider = models.CharField(
        max_length=32,
        default="wishart",
        help_text="Remote provider namespace for upstream query ids.",
    )
    remote_query_id = models.TextField(
        null=True, blank=True, help_text="Upstream query id once submitted."
    )
    payload_hash = models.TextField(help_text="Hash of the submitted payload.")
    state = models.CharField(
        max_length=16, choices=QueryState.choices, default=QueryState.SUBMITTED
    )
    next_poll_at = models.DateTimeField(null=True, blank=True)
    raw_response = models.JSONField(
        null=True, blank=True, help_text="Latest raw upstream response as JSONB."
    )
    # The TRUE outbound send time (F-18 freshness key), copied from the
    # submission's ``request_started_at`` at submit time — NOT the response-persist
    # time. ``default=timezone.now`` keeps it auto-populated for callers that don't
    # set it, while ALLOWING the worker to stamp the real send time (which
    # ``auto_now_add`` would have silently ignored), so an OLDER query completing
    # LATE never supersedes a NEWER one.
    submitted_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "remote_queries"
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "remote_query_id"],
                name="remote_query_provider_remote_id_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["state", "next_poll_at"], name="remote_query_poll_idx"),
        ]

    def __str__(self) -> str:
        return f"query:{self.remote_query_id or self.pk} ({self.state})"


class RemoteQueryItem(models.Model):
    """Maps a RemoteQuery to the RemoteTask/BatchItem/compound it resolves."""

    query = models.ForeignKey(
        RemoteQuery, on_delete=models.CASCADE, related_name="items"
    )
    task = models.ForeignKey(
        RemoteTask,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="query_items",
    )
    batch_item = models.ForeignKey(
        "batches.BatchItem",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="query_items",
    )
    compound = models.ForeignKey(
        "compounds.CompoundStructure",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="query_items",
    )
    inchikey = models.CharField(
        max_length=27, null=True, blank=True, help_text="InChIKey this mapping targets."
    )

    # --- durable per-member apply outcome (Task 15) ---
    # Each member of a multi-structure query carries its OWN terminal apply state
    # so a worker that resolves the query for the members it owns does NOT
    # silently skip a sibling owned by another worker: that sibling is parked
    # PENDING_OWNER until its owner applies it. The query only goes COMPLETE once
    # every member is in a terminal apply state (APPLIED/MISMATCH/FAILED).
    apply_state = models.CharField(
        max_length=24,
        choices=RemoteQueryItemApplyState.choices,
        default=RemoteQueryItemApplyState.PENDING,
        db_default=RemoteQueryItemApplyState.PENDING,
        help_text="PENDING/APPLIED/PENDING_OWNER/MISMATCH/FAILED.",
    )
    apply_detail = models.TextField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "remote_query_items"
        constraints = [
            models.CheckConstraint(
                name="remote_query_item_inchikey_format",
                condition=Q(inchikey__isnull=True) | Q(inchikey__regex=INCHIKEY_REGEX),
            ),
            models.UniqueConstraint(
                fields=["query", "task"],
                name="remote_query_item_query_task_unique",
            ),
            # One member per (query, inchikey): a stale worker cannot re-link a
            # query member it does not own, and the positional pairing in
            # _apply_query_result stays unambiguous (F-07).
            models.UniqueConstraint(
                fields=["query", "inchikey"],
                condition=Q(inchikey__isnull=False),
                name="remote_query_item_query_inchikey_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["query"], name="remote_query_item_query_idx"),
        ]

    def __str__(self) -> str:
        return f"qitem:{self.query_id}->{self.inchikey}"


class RemoteSubmission(models.Model):
    """Transactional submission outbox / idempotency record (F-06).

    Persists submission INTENT to the DB *before* the network call so a crash
    after the provider POST but before the local commit cannot cause a second
    POST. The deterministic idempotency key ``(provider, payload_hash)`` —
    ``payload_hash`` = sha256 hex of the canonical request payload — is unique
    over the live (non-FAILED) states via a partial-UNIQUE index, so a retry of
    the same payload reconciles against the existing row instead of re-POSTing.

    Lifecycle: PENDING (intent written) → SUBMITTED (provider id recorded,
    RemoteQuery linked, same txn) | UNKNOWN (in-doubt: provider may have accepted
    but acceptance is unconfirmed — key HELD, never auto-re-POSTed) | FAILED (no
    id obtained; key reusable). UNKNOWN is operator-resolved via
    ``manage.py resolve_submission``.
    """

    provider = models.CharField(
        max_length=32,
        default="wishart",
        help_text="Remote provider namespace.",
    )
    payload_hash = models.CharField(
        max_length=64,
        help_text="sha256 hex of the canonical request payload (idempotency key).",
    )
    state = models.CharField(
        max_length=16,
        choices=SubmissionState.choices,
        default=SubmissionState.PENDING,
    )
    query = models.ForeignKey(
        RemoteQuery,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="submissions",
        help_text="Linked RemoteQuery once the POST is confirmed (SUBMITTED).",
    )
    remote_query_id = models.TextField(
        null=True,
        blank=True,
        help_text="Provider query id captured on a confirmed submit.",
    )
    request_payload = models.JSONField(
        help_text="Canonical request payload submitted to the provider (JSONB).",
    )
    request_started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "TRUE outbound send time, stamped immediately before the /queries "
            "POST is attempted. Copied to the linked RemoteQuery.submitted_at so "
            "freshness orders by real send time, not response-persist time (F-18)."
        ),
    )
    reconcile_attempts = models.IntegerField(
        default=0,
        help_text=(
            "How many times an in-doubt PENDING intent has been reconciled "
            "without resolving. Past the cap (or TTL) the intent is aged out to "
            "FAILED so exactly one genuine re-POST may occur."
        ),
    )
    detail = models.TextField(
        null=True, blank=True, help_text="Failure detail when FAILED."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "remote_submissions"
        constraints = [
            # Idempotency: at most ONE live (PENDING/SUBMITTED) row per
            # (provider, payload_hash). FAILED rows do not conflict so the key
            # is reusable after a permanent submit failure.
            models.UniqueConstraint(
                fields=["provider", "payload_hash"],
                name="remote_submission_idempotency_unique",
                condition=Q(state__in=LIVE_SUBMISSION_STATES),
            ),
        ]
        indexes = [
            models.Index(
                fields=["provider", "payload_hash"],
                name="remote_submission_key_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"submission:{self.provider}:{self.payload_hash[:12]} ({self.state})"


class RemoteSubmissionMember(models.Model):
    """Durable live reservation for one submitted provider member (R-01).

    The ``RemoteSubmission`` idempotency key is the hash of the WHOLE batch
    payload, so regrouping an ambiguous batch ``[A, B]`` into ``[A, C]`` produces
    a different key and would let the same normalized member ``A`` be POSTed a
    second time while its first submission is still live. This per-member
    reservation closes that gap: a member is reserved (``active=True``) for as
    long as its submission holds the idempotency slot (PENDING/SUBMITTED/UNKNOWN),
    and the partial-UNIQUE over ``(provider, inchikey, structure_hash)`` for
    ``active`` rows means the worker can never re-POST the same normalized member
    in a different batch. A reservation is released deliberately — when its
    submission FAILS (key freed), when every member of a SUBMITTED query reaches
    a terminal apply state, or when an operator marks an UNKNOWN submission
    failed — never on an automatic PENDING→UNKNOWN in-doubt transition.
    """

    submission = models.ForeignKey(
        RemoteSubmission,
        on_delete=models.CASCADE,
        related_name="members",
    )
    provider = models.CharField(max_length=32, default="wishart")
    inchikey = models.CharField(max_length=27)
    structure_hash = models.CharField(
        max_length=64,
        help_text="sha256 hex of the submitted structure (SMILES/InChI).",
    )
    active = models.BooleanField(default=True, db_default=True)
    released_at = models.DateTimeField(null=True, blank=True)
    release_reason = models.CharField(max_length=64, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "remote_submission_members"
        constraints = [
            models.CheckConstraint(
                name="remote_submission_member_inchikey_format",
                condition=Q(inchikey__regex=INCHIKEY_REGEX),
            ),
            # At most ONE active reservation per normalized member: a regrouped
            # batch cannot repost a member already live in another submission.
            models.UniqueConstraint(
                fields=["provider", "inchikey", "structure_hash"],
                condition=Q(active=True),
                name="remote_submission_member_live_unique",
            ),
        ]
        indexes = [
            models.Index(
                fields=["provider", "inchikey", "active"],
                name="remote_sub_member_live_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"sub-member:{self.inchikey} (active={self.active})"


class RemoteRateBucket(models.Model):
    """Shared DB-backed token bucket for global rate limiting (F-08).

    A single row per ``(provider, bucket)`` holds the live token count; every
    worker draws tokens atomically (``UPDATE ... SET tokens = tokens - 1 WHERE
    tokens >= 1 RETURNING`` style) so N workers across processes respect ONE
    global rate. Tokens refill lazily from ``refill_rate_per_sec`` based on
    ``updated_at`` elapsed, capped at ``capacity``. A server ``Retry-After`` parks
    the bucket until ``blocked_until`` regardless of token count.
    """

    provider = models.CharField(max_length=32, default="wishart")
    bucket = models.CharField(
        max_length=32, help_text="Bucket name, e.g. 'entity' or 'submit'."
    )
    capacity = models.IntegerField(help_text="Max tokens (burst size).")
    tokens = models.FloatField(help_text="Current available token count.")
    refill_rate_per_sec = models.FloatField(
        help_text="Tokens added per second (per-minute budget / 60)."
    )
    blocked_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Bucket parked until this time (server Retry-After).",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "remote_rate_buckets"
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "bucket"],
                name="remote_rate_bucket_provider_bucket_unique",
            ),
            models.CheckConstraint(
                name="remote_rate_bucket_capacity_nonneg",
                condition=Q(capacity__gte=0),
            ),
        ]

    def __str__(self) -> str:
        return f"bucket:{self.provider}:{self.bucket} ({self.tokens:.2f})"


class RemoteCircuit(models.Model):
    """Shared DB-backed circuit breaker state (F-08).

    One row per provider; all workers share it. ``state`` is CLOSED/OPEN/
    HALF_OPEN. Consecutive failures (``consecutive_failures``) past the threshold
    OPEN the circuit. After ``reset_timeout`` a single worker wins the HALF_OPEN
    probe via a conditional UPDATE (``WHERE state='OPEN' AND opened_at <= cutoff``
    flipping to HALF_OPEN), so only one probe is issued — no thundering herd.
    """

    provider = models.CharField(max_length=32, default="wishart", unique=True)
    state = models.CharField(
        max_length=16,
        choices=CircuitState.choices,
        default=CircuitState.CLOSED,
    )
    consecutive_failures = models.IntegerField(default=0)
    opened_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "remote_circuits"
        constraints = [
            models.CheckConstraint(
                name="remote_circuit_failures_nonneg",
                condition=Q(consecutive_failures__gte=0),
            ),
        ]

    def __str__(self) -> str:
        return f"circuit:{self.provider} ({self.state})"


class RemoteAttempt(models.Model):
    """Append-only audit log of every remote call attempt (F-10).

    One row per submit/poll/entity attempt records the raw provider payload and
    the explicit HTTP status classification so failures are auditable. The table
    is never updated in place — each attempt is a fresh row — so the full
    request/response history of a query survives crashes and retries.
    """

    OPERATION_ENTITY = "ENTITY"
    OPERATION_SUBMIT = "SUBMIT"
    OPERATION_POLL = "POLL"
    OPERATION_CHOICES = (
        (OPERATION_ENTITY, "Entity lookup"),
        (OPERATION_SUBMIT, "Submit"),
        (OPERATION_POLL, "Poll"),
    )

    provider = models.CharField(max_length=32, default="wishart")
    operation = models.CharField(max_length=16, choices=OPERATION_CHOICES)
    query = models.ForeignKey(
        RemoteQuery,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="attempts",
    )
    task = models.ForeignKey(
        RemoteTask,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="attempts",
        help_text="The task whose identity this attempt was made for, if any.",
    )
    submission = models.ForeignKey(
        "remote.RemoteSubmission",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="attempts",
        help_text="The submission this attempt belongs to (submit/reconcile).",
    )
    remote_query_id = models.TextField(null=True, blank=True)
    http_status = models.IntegerField(
        null=True, blank=True, help_text="HTTP status code, if any."
    )
    classification = models.CharField(
        max_length=24,
        help_text="Explicit outcome class (e.g. FOUND, RETRYABLE, AUTH_FAILURE).",
    )
    request_inchikey = models.CharField(
        max_length=27,
        null=True,
        blank=True,
        help_text="Full 27-char InChIKey this attempt was made FOR, if any.",
    )
    returned_inchikey = models.CharField(
        max_length=27,
        null=True,
        blank=True,
        help_text="Full 27-char InChIKey the parsed response reported, if any.",
    )
    request_payload_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text="SHA-256 of the canonical request payload (submit batches).",
    )
    request_structure_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text="SHA-256 of the structure involved, where applicable.",
    )
    raw_payload = models.JSONField(
        null=True,
        blank=True,
        help_text="Raw provider response payload for THIS attempt (JSONB).",
    )
    raw_body_text = models.TextField(
        null=True,
        blank=True,
        help_text="Raw response BODY text (preserves non-JSON bodies verbatim).",
    )
    raw_body_sha256 = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text="SHA-256 hex digest of raw_body_text.",
    )
    safe_headers = models.JSONField(
        null=True,
        blank=True,
        help_text="Allow-listed response headers (content-type, retry-after, ...).",
    )
    detail = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "remote_attempts"
        indexes = [
            models.Index(
                fields=["query", "created_at"], name="remote_attempt_query_idx"
            ),
        ]
        constraints = [
            models.CheckConstraint(
                name="remote_attempt_request_inchikey_len27",
                condition=Q(request_inchikey__isnull=True)
                | Q(request_inchikey__length=27),
            ),
            models.CheckConstraint(
                name="remote_attempt_returned_inchikey_len27",
                condition=Q(returned_inchikey__isnull=True)
                | Q(returned_inchikey__length=27),
            ),
        ]

    def __str__(self) -> str:
        return f"attempt:{self.operation}:{self.classification}"


class RemoteBudgetLedger(models.Model):
    """Append-only remote-budget reservation ledger."""

    principal = models.CharField(max_length=128)
    units = models.IntegerField(default=1)
    reason = models.CharField(max_length=64, default="remote_task")
    task = models.ForeignKey(
        RemoteTask,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="budget_reservations",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "remote_budget_ledger"
        indexes = [
            models.Index(
                fields=["principal", "created_at"],
                name="remote_budget_princ_day_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                name="remote_budget_units_positive",
                condition=Q(units__gt=0),
            ),
        ]

    def __str__(self) -> str:
        return f"remote-budget:{self.principal}:{self.units}"


class RemotePrincipalState(models.Model):
    """Fair-scheduling state for a remote-work principal."""

    principal = models.CharField(max_length=128, unique=True)
    last_claimed_at = models.DateTimeField(null=True, blank=True)
    claim_count = models.IntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "remote_principal_states"
        indexes = [
            models.Index(
                fields=["last_claimed_at", "principal"],
                name="remote_principal_claim_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                name="remote_principal_claim_count_nonneg",
                condition=Q(claim_count__gte=0),
            ),
        ]

    def __str__(self) -> str:
        return f"remote-principal:{self.principal}"


class RemoteQueueControl(models.Model):
    """Serialization anchor for the global remote-queue depth gate (H-05).

    A single row per provider. ``enqueue_task`` takes a ``SELECT ... FOR UPDATE``
    on this row BEFORE counting active tasks and inserting, so N concurrent
    enqueuers across processes are serialized: the count-then-insert can never
    race past ``REMOTE_MAX_QUEUE_DEPTH``. The row carries no real payload — its
    only job is to be the lock that makes the high-water check atomic.
    """

    provider = models.CharField(max_length=32, default="wishart", unique=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "remote_queue_controls"

    def __str__(self) -> str:
        return f"remote-queue-control:{self.provider}"


class RemoteProviderControl(models.Model):
    """Durable operator-controlled gate for a remote provider (one row each).

    A provider that returns 401/403 is flipped to ``AUTH_BLOCKED`` here so EVERY
    worker process stops calling it (``RemoteWorker.tick`` short-circuits on this
    row) until an operator runs ``manage.py reset_provider_auth``. The unique
    ``provider`` column serializes concurrent block writers via
    ``update_or_create`` — the first creates the row, the rest update it.
    """

    provider = models.CharField(max_length=32, default="wishart", unique=True)
    state = models.CharField(
        max_length=24,
        choices=ProviderControlState.choices,
        default=ProviderControlState.ENABLED,
    )
    blocked_reason = models.TextField(null=True, blank=True)
    credential_version = models.CharField(max_length=128, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "remote_provider_controls"

    def __str__(self) -> str:
        return f"remote-provider-control:{self.provider}:{self.state}"
