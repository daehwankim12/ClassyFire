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

from apps.core.enums import (
    ACTIVE_TASK_STATES,
    LIVE_SUBMISSION_STATES,
    CircuitState,
    QueryState,
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
    submitted_at = models.DateTimeField(auto_now_add=True)
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
    RemoteQuery linked, same txn) | FAILED (no id obtained; key reusable).
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
    remote_query_id = models.TextField(null=True, blank=True)
    http_status = models.IntegerField(
        null=True, blank=True, help_text="HTTP status code, if any."
    )
    classification = models.CharField(
        max_length=24,
        help_text="Explicit outcome class (e.g. FOUND, RETRYABLE, AUTH_FAILURE).",
    )
    raw_payload = models.JSONField(
        null=True,
        blank=True,
        help_text="Raw provider response payload for THIS attempt (JSONB).",
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
