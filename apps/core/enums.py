"""Canonical enums and state machines for ClassyFire Batch Cache Web.

Every enum here is a :class:`django.db.models.TextChoices` with stable
UPPERCASE string values. These strings are the contract: a future Go/Rust
service that reads/writes the SAME Postgres schema must use these exact
literals. NEVER switch to IntEnum/ordinals — the persisted value is the string.

State machines live HERE and only here. ``docs/state-machines.md`` documents
the allowed transitions; this module is the executable source of truth for the
allowed-state SETS used by DB constraints (e.g. the active-task partial-unique
index).
"""
from __future__ import annotations

from django.db import models


class SourceType(models.TextChoices):
    """Origin of a ClassyFire result row (live cache / archive / official)."""

    WISHART_LIVE_QUERY = "WISHART_LIVE_QUERY", "Wishart live query"
    WISHART_LIVE_ENTITY = "WISHART_LIVE_ENTITY", "Wishart live entity"
    ARCHIVED_WISHART_JSON = "ARCHIVED_WISHART_JSON", "Archived Wishart JSON"
    LAB_OFFICIAL_IMPORT = "LAB_OFFICIAL_IMPORT", "Lab official import"
    HISTORICAL_AGGREGATE = "HISTORICAL_AGGREGATE", "Historical aggregate"


# Higher number = higher trust. A new result only overwrites the current live
# row when its priority is >= the stored row's priority. Stored as an INTEGER
# column on classyfire_live (source_priority) so other languages can compare it
# without re-deriving from this map.
SOURCE_PRIORITY: dict[str, int] = {
    SourceType.WISHART_LIVE_QUERY: 100,
    SourceType.WISHART_LIVE_ENTITY: 100,
    SourceType.ARCHIVED_WISHART_JSON: 90,
    SourceType.LAB_OFFICIAL_IMPORT: 85,
    SourceType.HISTORICAL_AGGREGATE: 70,
}


class ResultKind(models.TextChoices):
    """Disposition of a single immutable ``classification_results`` event.

    Every write to the classification store appends exactly one
    ``ClassificationResult`` row (one INSERT, never UPDATE/DELETE). ``kind``
    records what happened to that event *relative to the live pointer* at write
    time so a future Go/Rust reader can audit the full history:

    * ``APPLIED`` — the event materialized the current live row (no prior row, or
      it claimed a previously-empty slot).
    * ``SUPERSEDED`` — the event overwrote an existing live row (prior row was
      archived to ``classification_history`` first); the new event is now live.
    * ``REJECTED_MISMATCH`` — the source returned a different InChIKey than the
      one requested. The mismatch is STORED (not discarded); live is unchanged.
    * ``REJECTED_LOWER_PRIORITY`` — a strictly higher-trust row already exists;
      the event is recorded but live is unchanged.
    * ``REJECTED_STALE`` — same priority but the existing live row is newer or
      equal; the event is recorded but live is unchanged.
    """

    APPLIED = "APPLIED", "Applied"
    SUPERSEDED = "SUPERSEDED", "Superseded"
    REJECTED_MISMATCH = "REJECTED_MISMATCH", "Rejected (key mismatch)"
    REJECTED_LOWER_PRIORITY = "REJECTED_LOWER_PRIORITY", "Rejected (lower priority)"
    REJECTED_STALE = "REJECTED_STALE", "Rejected (stale)"


# Result kinds that materialized the current live row (live.result points here).
APPLIED_RESULT_KINDS: tuple[str, ...] = (
    ResultKind.APPLIED.value,
    ResultKind.SUPERSEDED.value,
)


class BatchJobStatus(models.TextChoices):
    """Coarse lifecycle status for a batch job (batch_jobs.status).

    State machine (see docs/state-machines.md):
      PENDING → IN_PROGRESS → COMPLETE (all items resolved)
                            ├→ COMPLETE_WITH_ACTION_REQUIRED (user action needed)
                            └→ FAILED   (unrecoverable error)
    """

    PENDING = "PENDING", "Pending"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    COMPLETE = "COMPLETE", "Complete"
    COMPLETE_WITH_ACTION_REQUIRED = (
        "COMPLETE_WITH_ACTION_REQUIRED",
        "Complete with action required",
    )
    FAILED = "FAILED", "Failed"


class BatchProcessingState(models.TextChoices):
    """Fine-grained ingest-worker state machine for a batch job.

    Lives in an explicit ``batch_jobs.processing_state`` column SEPARATE from the
    coarse user-facing ``status`` (:class:`BatchJobStatus`). A future Go/Rust
    worker drives the SAME column with the SAME string literals.

    State machine (see docs/state-machines.md §7):
      UPLOADING ──▶ PARSING ──▶ NORMALIZING ──▶ LOCAL_LOOKUP
                                                   ├─▶ REMOTE_PENDING  (remote work outstanding)
                                                   ├─▶ ACTION_REQUIRED (only user-structure items remain)
                                                   └─▶ COMPLETE        (everything resolved)
      any ──▶ FAILED (unrecoverable error; processing_error records the reason)

    Coarse-status mapping (kept in sync by recompute_job_counters):
      UPLOADING / PARSING / NORMALIZING / LOCAL_LOOKUP / REMOTE_PENDING → IN_PROGRESS
      ACTION_REQUIRED → COMPLETE_WITH_ACTION_REQUIRED
      COMPLETE        → COMPLETE
      FAILED          → FAILED

    The durable worker CLAIMS jobs for processing via ``SELECT ... FOR UPDATE
    SKIP LOCKED`` over the states that still need work and advances them.
    """

    UPLOADING = "UPLOADING", "Uploading"
    PARSING = "PARSING", "Parsing"
    NORMALIZING = "NORMALIZING", "Normalizing"
    LOCAL_LOOKUP = "LOCAL_LOOKUP", "Local lookup"
    REMOTE_PENDING = "REMOTE_PENDING", "Remote pending"
    ACTION_REQUIRED = "ACTION_REQUIRED", "Action required"
    COMPLETE = "COMPLETE", "Complete"
    FAILED = "FAILED", "Failed"

    @classmethod
    def claimable_states(cls) -> tuple[BatchProcessingState, ...]:
        """States whose jobs still need ingest-worker processing.

        A job sitting in any of these is unfinished local-processing work the
        durable worker should claim. PARSING is included so a job whose items
        were persisted in-request (PENDING_NORMALIZATION) but not yet normalized
        is picked up; NORMALIZING/LOCAL_LOOKUP cover a worker that crashed
        mid-run and must resume from its checkpoint.
        """
        return (cls.UPLOADING, cls.PARSING, cls.NORMALIZING, cls.LOCAL_LOOKUP)


# Raw string values of the processing states the ingest worker may claim, handy
# for ``state__in=...`` filters and other-language ports.
CLAIMABLE_PROCESSING_STATES: tuple[str, ...] = tuple(
    s.value for s in BatchProcessingState.claimable_states()
)


class DetectedInputType(models.TextChoices):
    """Detected shape of a single batch input row before normalization."""

    INCHIKEY = "INCHIKEY", "InChIKey"
    INCHI = "INCHI", "InChI"
    SMILES = "SMILES", "SMILES"
    SDF = "SDF", "SDF"
    UNKNOWN = "UNKNOWN", "Unknown"


class BatchItemStatus(models.TextChoices):
    """User-facing status of a batch item (requirements section 6.2).

    Transitions are documented in docs/state-machines.md. Terminal statuses are
    the COMPLETE/INVALID/MISMATCH outcomes plus the FOUND_* cache-hit states.
    """

    PENDING_NORMALIZATION = "PENDING_NORMALIZATION", "Pending normalization"
    FOUND_LIVE_CACHE = "FOUND_LIVE_CACHE", "Found in live cache"
    FOUND_SEED_CACHE = "FOUND_SEED_CACHE", "Found in seed cache"
    FOUND_WISHART_ENTITY = "FOUND_WISHART_ENTITY", "Found Wishart entity"
    PENDING_CLASSIFICATION = "PENDING_CLASSIFICATION", "Pending classification"
    COMPLETE_AFTER_WISHART = "COMPLETE_AFTER_WISHART", "Complete after Wishart"
    NEED_STRUCTURE = "NEED_STRUCTURE", "Needs structure"
    REMOTE_DELAYED = "REMOTE_DELAYED", "Remote delayed"
    INVALID_INPUT = "INVALID_INPUT", "Invalid input"
    RESULT_KEY_MISMATCH = "RESULT_KEY_MISMATCH", "Result key mismatch"


class TaskState(models.TextChoices):
    """remote_tasks lifecycle (requirements section 6.3).

    The queue is drained with ``FOR UPDATE SKIP LOCKED`` over remote_tasks using
    the (state, priority, next_run_at, lock_owner, locked_at, attempt) columns.
    See docs/state-machines.md for the full claim protocol.

    Active vs terminal
    ------------------
    Only ``DONE`` is terminal. Every other state — including ``DEFERRED`` —
    is "active" and therefore holds the per-InChIKey dedup slot enforced by the
    partial-unique index. ``DEFERRED`` is treated as active on purpose: a task
    parked for later still represents outstanding work for that InChIKey, so we
    must not let a duplicate task be created alongside it.
    """

    READY = "READY", "Ready"
    LOCKED = "LOCKED", "Locked"
    RUNNING = "RUNNING", "Running"
    ENTITY_LOOKUP = "ENTITY_LOOKUP", "Entity lookup"
    SUBMIT_WAIT = "SUBMIT_WAIT", "Submit wait"
    SUBMITTED = "SUBMITTED", "Submitted"
    POLL_WAIT = "POLL_WAIT", "Poll wait"
    RETRY_WAIT = "RETRY_WAIT", "Retry wait"
    DEFERRED = "DEFERRED", "Deferred"
    DONE = "DONE", "Done"

    @classmethod
    def active_states(cls) -> tuple[TaskState, ...]:
        """States that hold the per-InChIKey dedup slot (everything but DONE)."""
        return tuple(state for state in cls if state != cls.DONE)


class QueryState(models.TextChoices):
    """remote_queries lifecycle for a single submitted ClassyFire query/batch.

    SUBMITTED -> POLLING -> COMPLETE (success) | FAILED (terminal error).
    See docs/state-machines.md.
    """

    SUBMITTED = "SUBMITTED", "Submitted"
    POLLING = "POLLING", "Polling"
    COMPLETE = "COMPLETE", "Complete"
    FAILED = "FAILED", "Failed"


class SubmissionState(models.TextChoices):
    """remote_submissions outbox lifecycle (F-06 transactional submission).

    The submission outbox makes a /queries POST crash-safe and idempotent. A row
    is written with state=PENDING *before* the network call. After the POST
    succeeds the SAME transaction flips it to SUBMITTED and links the RemoteQuery.
    A retryable failure (no provider id obtained) flips it to FAILED so the
    deterministic idempotency key ``(provider, payload_hash)`` can be reused by a
    later submit. The partial-UNIQUE over the non-FAILED states guarantees that a
    crash between intent and confirmation cannot cause a second POST: the next
    worker tick finds the existing live row and reconciles instead.

    UNKNOWN is the in-doubt terminus: when a PENDING intent cannot be confirmed
    (the provider may have ACCEPTED the POST but the socket dropped, then the
    reconcile cap / TTL elapsed) it is parked as UNKNOWN rather than FAILED.
    UNKNOWN is a LIVE state — it HOLDS the idempotency key — so the worker NEVER
    auto-re-POSTs the same structures (which would duplicate the upstream
    submission). An operator (``manage.py resolve_submission``) resolves it:
    confirm acceptance → SUBMITTED, or verify non-acceptance → FAILED (key freed).

    State machine:
      PENDING ──success───> SUBMITTED (terminal: links RemoteQuery)
              ──failure───> FAILED    (terminal: key reusable)
              ──in-doubt──> UNKNOWN   (parked: key HELD; operator-resolved only)
      UNKNOWN ──operator confirmed acceptance──> SUBMITTED
              ──operator verified non-acceptance──> FAILED (key freed)
    """

    PENDING = "PENDING", "Pending"
    SUBMITTED = "SUBMITTED", "Submitted"
    UNKNOWN = "UNKNOWN", "Unknown provider acceptance"
    FAILED = "FAILED", "Failed"

    @classmethod
    def live_states(cls) -> tuple[SubmissionState, ...]:
        """States that hold the idempotency slot (everything but FAILED).

        UNKNOWN is included so an in-doubt intent keeps the partial-UNIQUE slot
        and is never auto-re-POSTed while provider acceptance is ambiguous.
        """
        return (cls.PENDING, cls.SUBMITTED, cls.UNKNOWN)


# Raw string values of the submission states that hold the idempotency slot,
# handy for partial-UNIQUE constraints and other-language ports.
LIVE_SUBMISSION_STATES: tuple[str, ...] = tuple(
    s.value for s in SubmissionState.live_states()
)


class RemoteQueryItemApplyState(models.TextChoices):
    """Durable per-member apply outcome for a ``remote_query_items`` row (Task 15).

    Each member of a multi-structure query carries its OWN terminal apply state
    so a worker that resolves the query for the members it owns does NOT silently
    skip a sibling owned by another worker: the sibling is parked ``PENDING_OWNER``
    until its owner applies it. The query only goes COMPLETE once every member is
    in a terminal apply state (``APPLIED``/``MISMATCH``/``FAILED``).

    value == name on purpose: these literals are the persisted contract (existing
    rows + the COMPLETE-gating predicate). A future Go/Rust worker drives the SAME
    column with the SAME strings.
    """

    PENDING = "PENDING", "Pending"
    APPLIED = "APPLIED", "Applied"
    PENDING_OWNER = "PENDING_OWNER", "Pending owner"
    MISMATCH = "MISMATCH", "Mismatch"
    FAILED = "FAILED", "Failed"


class CircuitState(models.TextChoices):
    """remote_circuits breaker lifecycle (F-08 DB-backed circuit breaker).

    State lives in Postgres (one row per provider) so N workers across processes
    share ONE breaker. Consecutive failures open the circuit; after a cool-down
    a single worker wins the HALF_OPEN probe via a conditional UPDATE (no
    thundering herd). See docs/state-machines.md §6.

    CLOSED ──failures>=threshold──> OPEN
    OPEN   ──cooldown elapsed (one probe wins)──> HALF_OPEN
    HALF_OPEN ──probe success──> CLOSED
              ──probe failure──> OPEN
    """

    CLOSED = "CLOSED", "Closed"
    OPEN = "OPEN", "Open"
    HALF_OPEN = "HALF_OPEN", "Half open"


class ProviderControlState(models.TextChoices):
    """Operator-controlled remote-provider gate (one row per provider).

    Lives in ``remote_provider_controls`` so a single durable state stops ALL
    worker processes from calling a provider whose credentials are bad. When a
    remote call returns 401/403 the worker flips the provider to
    ``AUTH_BLOCKED``; every worker's ``tick()`` then short-circuits until an
    operator runs ``manage.py reset_provider_auth`` (back to ``ENABLED``). This
    prevents N workers from hammering a provider with rejected credentials.

    ENABLED ──provider 401/403──> AUTH_BLOCKED
    AUTH_BLOCKED ──operator reset──> ENABLED
    """

    ENABLED = "ENABLED", "Enabled"
    AUTH_BLOCKED = "AUTH_BLOCKED", "Auth blocked"


class SeedProfile(models.TextChoices):
    """Which seed shape was imported."""

    FULL = "FULL", "Full"
    LITE = "LITE", "Lite"
    CUSTOM = "CUSTOM", "Custom"


class EventCategory(models.TextChoices):
    """Category for a row in system_events."""

    WORKER = "WORKER", "Worker"
    REMOTE = "REMOTE", "Remote"
    BACKUP = "BACKUP", "Backup"
    SEED = "SEED", "Seed"
    SECURITY = "SECURITY", "Security"


# Convenience tuple of the raw string values for the active task states, handy
# for Q(state__in=...) constraints and for other-language ports.
ACTIVE_TASK_STATES: tuple[str, ...] = tuple(s.value for s in TaskState.active_states())
