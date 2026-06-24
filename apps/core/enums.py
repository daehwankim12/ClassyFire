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
