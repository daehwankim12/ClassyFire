"""Batch job creation and local processing service.

Public interface
----------------
create_job(
    rows: Iterable[dict],
    *,
    remote_enabled: bool = True,
) -> tuple[BatchJob, str]
    Creates a BatchJob, ingests rows, runs local processing synchronously,
    updates counters, and returns (job, plaintext_secret).

create_job_records(
    rows: Iterable[dict],
    *,
    remote_enabled: bool = True,
) -> tuple[BatchJob, str]
    Creates only the BatchJob and raw BatchItems in one transaction, returning
    (job, plaintext_secret). Call process_job_items(job) separately.

process_job_items(job: BatchJob) -> None
    Runs local normalization + lookup for every PENDING_NORMALIZATION item
    in the job, updating statuses, creating compounds, and scheduling remote
    tasks as needed.

Local processing flow per row
------------------------------
1. normalize_row(raw) → NormalizedRow
2. If error → INVALID_INPUT (message = error)
3. If inchikey (27-char) → lookup_current_classification(inchikey)
   - LIVE hit → FOUND_LIVE_CACHE
   - SEED hit → FOUND_SEED_CACHE
   - miss → see step 4
4. Local miss:
   - Create/reuse CompoundStructure; update CompoundUsage.
   - If detected_type is INCHIKEY (no structure):
       * remote_enabled=False → NEED_STRUCTURE
       * remote_enabled=True  → PENDING_CLASSIFICATION + create/reuse RemoteTask
   - If has canonical_smiles/inchi (structure known):
       * PENDING_CLASSIFICATION + create/reuse RemoteTask
5. Update job counters and status.

Global dedupe of RemoteTask
----------------------------
The remote_tasks table has a partial unique constraint: only ONE active task
per inchikey.  When creating a RemoteTask, catch IntegrityError in a
savepoint (or use get_or_create over the active states), reuse the existing
task, and leave the item PENDING_CLASSIFICATION.  This is expected; the
worker fans results out to all waiting items in Task 9.
"""
from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterable
from typing import cast

from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.batches.ingest import ingest_rows
from apps.batches.models import BatchItem, BatchJob
from apps.classifications.services import (
    ClassificationLookupResult,
    LookupTier,
    classification_lookup_snapshot,
    lookup_current_classification,
    lookup_current_classifications,
)
from apps.compounds.models import CompoundStructure, CompoundUsage
from apps.compounds.validators import NormalizedRow, normalize_row
from apps.core.enums import (
    CLAIMABLE_PROCESSING_STATES,
    BatchItemStatus,
    BatchJobStatus,
    BatchProcessingState,
    DetectedInputType,
    TaskState,
)
from apps.core.security import MAX_STRUCTURE_LEN, RowTooLongError, check_structure_length
from apps.remote.models import RemoteTask

# Number of characters preserved in the bounded preview for an over-limit row.
_OVERLONG_PREVIEW_CHARS = 200


def _pin_resolved_classification(item: BatchItem, snapshot: dict | None) -> None:
    """Pin ``item.resolved_classification`` from a lookup snapshot's event id.

    The snapshot carries ``result_id`` — the PK of the immutable
    ClassificationResult event that materialized the current live row. Seed-tier
    hits have no event (``result_id`` is None) and leave the FK null; the pinned
    ``classification_snapshot`` JSON remains the provenance for those.
    """
    result_id = snapshot.get("result_id") if isinstance(snapshot, dict) else None
    item.resolved_classification_id = result_id if result_id is not None else None


def _reconcile_resolved_classification(job: BatchJob) -> None:
    """Pin ``resolved_classification`` for resolved items missing the FK.

    The remote fan-out (apps/remote, a separate workstream) pins the
    ``classification_snapshot`` JSON — which now carries ``result_id`` — but does
    not set the FK directly. This batches-owned reconciliation runs inside every
    ``recompute_job_counters`` call (which the fan-out always invokes after
    flipping item statuses), so the FK is pinned for the Wishart-entity and
    remote-query-complete resolution paths too. Idempotent: only fills a null FK
    from a snapshot's event id; never overwrites or unsets a pinned event.
    """
    resolved_statuses = (
        BatchItemStatus.FOUND_LIVE_CACHE,
        BatchItemStatus.FOUND_SEED_CACHE,
        BatchItemStatus.FOUND_WISHART_ENTITY,
        BatchItemStatus.COMPLETE_AFTER_WISHART,
    )
    candidates = BatchItem.objects.filter(
        job=job,
        status__in=resolved_statuses,
        resolved_classification__isnull=True,
        classification_snapshot__isnull=False,
    ).only("pk", "classification_snapshot")
    for item in candidates:
        snapshot = item.classification_snapshot
        result_id = snapshot.get("result_id") if isinstance(snapshot, dict) else None
        if result_id is not None:
            BatchItem.objects.filter(
                pk=item.pk, resolved_classification__isnull=True
            ).update(resolved_classification_id=result_id)


class StructureSubmissionError(ValueError):
    """User-correctable structure submission error."""


class StructureSubmissionConflictError(StructureSubmissionError):
    """The target batch item is no longer eligible for structure submission."""


class StructureKeyMismatchError(StructureSubmissionError):
    """Submitted structure normalized to a different InChIKey than expected."""

    def __init__(self, submitted_inchikey: str, expected_inchikey: str) -> None:
        self.submitted_inchikey = submitted_inchikey
        self.expected_inchikey = expected_inchikey
        super().__init__(
            f"Submitted InChIKey {submitted_inchikey} does not match "
            f"expected {expected_inchikey}"
        )


def _generate_secret() -> tuple[str, str]:
    """Return (plaintext_secret, secret_hash).

    The plaintext is returned to the caller once; only the hash is persisted.
    Uses Django's make_password (pbkdf2_sha256) for the hash.
    """
    plaintext = secrets.token_urlsafe(48)  # ~64 chars, URL-safe
    hashed = make_password(plaintext)
    return plaintext, hashed


def _get_or_create_compound(
    inchikey: str,
    original_input: str,
    canonical_smiles: str | None,
    inchi: str | None,
) -> CompoundStructure | None:
    """Get or create a CompoundStructure for *inchikey*.

    Key-only inputs do not create blank structure rows.  If a blank row exists
    from older data, a later real structure fills the missing fields exactly
    once; existing populated structures still keep first-write-wins semantics.
    """
    if canonical_smiles is None and inchi is None:
        return None

    def fill_missing_structure(compound: CompoundStructure) -> CompoundStructure:
        if not compound.canonical_smiles and not compound.inchi:
            compound.original_input = original_input
            compound.original_smiles = canonical_smiles
            compound.canonical_smiles = canonical_smiles
            compound.inchi = inchi
            compound.save(
                update_fields=[
                    "original_input",
                    "original_smiles",
                    "canonical_smiles",
                    "inchi",
                ]
            )
        return compound

    with transaction.atomic():
        compound = (
            CompoundStructure.objects.select_for_update()
            .filter(inchikey=inchikey)
            .order_by("created_at", "pk")
            .first()
        )
        if compound is not None:
            return fill_missing_structure(compound)

        try:
            with transaction.atomic():
                return CompoundStructure.objects.create(
                    inchikey=inchikey,
                    original_input=original_input,
                    original_smiles=canonical_smiles,
                    canonical_smiles=canonical_smiles,
                    inchi=inchi,
                )
        except IntegrityError:
            compound = CompoundStructure.objects.select_for_update().get(
                inchikey=inchikey
            )
            return fill_missing_structure(compound)


def _update_usage(inchikey: str) -> None:
    """Upsert CompoundUsage for *inchikey*, incrementing seen_count atomically."""
    CompoundUsage.objects.get_or_create(
        inchikey=inchikey,
        defaults={"seen_count": 0},
    )
    # Atomic DB-side increment regardless of whether the row was just created or
    # already existed — avoids read-modify-write races on concurrent jobs.
    CompoundUsage.objects.filter(inchikey=inchikey).update(
        seen_count=F("seen_count") + 1,
        last_seen=timezone.now(),
    )


def _get_or_create_remote_task(
    inchikey: str,
    compound: CompoundStructure | None,
) -> RemoteTask:
    """Get or create an active RemoteTask for *inchikey* (global dedupe).

    The partial-unique constraint allows only ONE active task per inchikey.
    We try to create; on IntegrityError (duplicate active task) we fetch
    the existing one in a savepoint so the outer transaction is unaffected.
    """
    from apps.core.enums import ACTIVE_TASK_STATES

    # Try to find an existing active task first (avoids exception-path overhead
    # on the second job for the same inchikey).
    existing = RemoteTask.objects.filter(
        inchikey=inchikey,
        state__in=ACTIVE_TASK_STATES,
    ).first()
    if existing is not None:
        if compound is not None and existing.compound_id is None:
            RemoteTask.objects.filter(pk=existing.pk, compound_id__isnull=True).update(
                compound_id=compound.pk
            )
            existing.refresh_from_db(fields=["compound_id"])
        return existing

    # No active task found — create one in a savepoint.
    try:
        with transaction.atomic():
            return RemoteTask.objects.create(
                inchikey=inchikey,
                compound=compound,
                state=TaskState.READY,
            )
    except IntegrityError:
        # A concurrent create beat us; fetch the winner.
        existing = RemoteTask.objects.filter(
            inchikey=inchikey,
            state__in=ACTIVE_TASK_STATES,
        ).get()
        if compound is not None and existing.compound_id is None:
            RemoteTask.objects.filter(pk=existing.pk, compound_id__isnull=True).update(
                compound_id=compound.pk
            )
            existing.refresh_from_db(fields=["compound_id"])
        return existing


def submit_structure_for_item(item_id: int, structure: str) -> BatchItem:
    """Submit a missing structure for a NEED_STRUCTURE item.

    All item state transitions happen while holding a row lock so stale form/API
    submissions cannot overwrite a concurrent resolution.
    """
    try:
        check_structure_length(structure)
    except RowTooLongError as exc:
        raise StructureSubmissionError(str(exc)) from exc

    norm = normalize_row(structure)
    if norm.error or norm.inchikey is None:
        raise StructureSubmissionError(
            f"Could not parse structure: {norm.error or 'unknown'}"
        )
    if norm.canonical_smiles is None and norm.inchi is None:
        raise StructureSubmissionError(
            "Structure submission must include a parseable SMILES or InChI structure."
        )

    with transaction.atomic():
        item = (
            BatchItem.objects.select_for_update()
            .select_related("job")
            .get(pk=item_id)
        )
        job = item.job

        if item.status != BatchItemStatus.NEED_STRUCTURE:
            raise StructureSubmissionConflictError(
                "Structure can only be submitted for items that need a structure."
            )
        if item.classification_snapshot is not None:
            raise StructureSubmissionConflictError(
                "Item already has a classification snapshot."
            )
        if item.inchikey and norm.inchikey != item.inchikey:
            raise StructureKeyMismatchError(norm.inchikey, item.inchikey)

        resolved_inchikey = norm.inchikey
        item.inchikey = resolved_inchikey
        item.classification_snapshot = None
        item.resolved_classification_id = None

        result = lookup_current_classification(resolved_inchikey)
        if result is not None:
            item.classification_snapshot = classification_lookup_snapshot(result)
            _pin_resolved_classification(item, item.classification_snapshot)
            item.status = (
                BatchItemStatus.FOUND_LIVE_CACHE
                if result.tier == LookupTier.LIVE
                else BatchItemStatus.FOUND_SEED_CACHE
            )
            item.save(
                update_fields=[
                    "status",
                    "inchikey",
                    "classification_snapshot",
                    "resolved_classification",
                ]
            )
        else:
            compound = _get_or_create_compound(
                resolved_inchikey,
                structure,
                norm.canonical_smiles,
                norm.inchi,
            )
            item.compound = compound
            if job.remote_enabled:
                item.status = BatchItemStatus.PENDING_CLASSIFICATION
                item.message = None
                item.save(
                    update_fields=[
                        "status",
                        "inchikey",
                        "compound",
                        "message",
                        "classification_snapshot",
                        "resolved_classification",
                    ]
                )
                _get_or_create_remote_task(resolved_inchikey, compound)
            else:
                item.status = BatchItemStatus.REMOTE_DELAYED
                item.message = "Structure provided; remote classification disabled."
                item.save(
                    update_fields=[
                        "status",
                        "inchikey",
                        "compound",
                        "message",
                        "classification_snapshot",
                        "resolved_classification",
                    ]
                )

    recompute_job_counters(item.job_id)
    item.refresh_from_db()
    return item


# Sentinel: caller did NOT supply a pre-fetched bulk lookup for this item, so
# _process_item must do its own per-item DB lookup (the legacy path).
_NO_LOOKUP_OVERRIDE = object()


def _process_item(
    item: BatchItem,
    *,
    remote_enabled: bool,
    lookup_override: object = _NO_LOOKUP_OVERRIDE,
    norm_override: object = _NO_LOOKUP_OVERRIDE,
) -> BatchItemStatus:
    """Normalize + local lookup for a single item.  Returns the final status.

    Updates item fields (inchikey, detected_type, compound, message) in place;
    the caller is responsible for calling item.save().

    ``lookup_override``: when supplied (``None`` or a
    :class:`ClassificationLookupResult`), the per-item ``lookup_current_classification``
    DB call is SKIPPED and this pre-fetched value is used instead. The chunked
    worker passes the result of ONE bulk ``WHERE inchikey IN (...)`` query so
    the N+1 disappears while the resolution semantics stay identical.

    ``norm_override``: a pre-computed :class:`NormalizedRow` so the (expensive,
    RDKit-bound) normalization is not repeated by the chunk worker. When given,
    the caller MUST have already applied the structure-length guard; if the row
    is over the limit it returns ``None`` here and the worker handles the
    INVALID_INPUT transition. ``check_structure_length`` is therefore only run
    when ``norm_override`` is the sentinel (the legacy single-item path).
    """
    item.classification_snapshot = None
    item.resolved_classification_id = None

    if norm_override is _NO_LOOKUP_OVERRIDE:
        # Step 0: Guard — reject rows whose structure string exceeds
        # MAX_STRUCTURE_LEN. This check lives here (service layer) so it covers
        # BOTH the UI and API paths. For an over-limit row we preserve a bounded
        # preview of the original input (FR-004) rather than a synthetic
        # sentinel, and record an explicit message.
        try:
            check_structure_length(item.original_input)
        except RowTooLongError:
            raw_len = len(item.original_input)
            preview = item.original_input[:_OVERLONG_PREVIEW_CHARS]
            item.original_input = f"{preview}…[truncated, {raw_len} chars total]"
            item.message = (
                f"Row exceeds maximum length "
                f"({raw_len} chars; limit {MAX_STRUCTURE_LEN})"
            )
            return BatchItemStatus.INVALID_INPUT

        # Step 1: Normalize
        norm = normalize_row(item.original_input, external_id=item.external_id)
    else:
        norm = cast("NormalizedRow", norm_override)
    item.detected_type = norm.detected_type

    # Step 2: Handle normalization error
    if norm.error:
        item.message = norm.error
        return BatchItemStatus.INVALID_INPUT

    # Step 3: Lookup (only possible with a 27-char inchikey)
    if norm.inchikey and len(norm.inchikey) == 27:
        item.inchikey = norm.inchikey
        result: ClassificationLookupResult | None
        if lookup_override is _NO_LOOKUP_OVERRIDE:
            result = lookup_current_classification(norm.inchikey)
        else:
            result = cast("ClassificationLookupResult | None", lookup_override)
        if result is not None:
            item.classification_snapshot = classification_lookup_snapshot(result)
            _pin_resolved_classification(item, item.classification_snapshot)
            if result.tier == LookupTier.LIVE:
                return BatchItemStatus.FOUND_LIVE_CACHE
            else:
                return BatchItemStatus.FOUND_SEED_CACHE

        # Step 4: Local miss — create/reuse structure only if one was supplied.
        has_structure = norm.detected_type != DetectedInputType.INCHIKEY
        compound = (
            _get_or_create_compound(
                norm.inchikey,
                norm.original_input,
                norm.canonical_smiles,
                norm.inchi,
            )
            if has_structure
            else None
        )
        item.compound = compound
        _update_usage(norm.inchikey)

        if not remote_enabled:
            return (
                BatchItemStatus.REMOTE_DELAYED
                if has_structure
                else BatchItemStatus.NEED_STRUCTURE
            )

        # Create/reuse a RemoteTask. Key-only tasks are entity lookups; structure
        # tasks can proceed to calculation if entity lookup misses.
        _get_or_create_remote_task(norm.inchikey, compound)
        return BatchItemStatus.PENDING_CLASSIFICATION

    # inchikey is None after normalization → the input could not be parsed at all
    # (this shouldn't normally happen — normalize_row returns an error — but be safe)
    item.message = norm.error or "Could not derive InChIKey from input"
    return BatchItemStatus.INVALID_INPUT


def recompute_job_counters(job_id: uuid.UUID | str) -> None:
    """Recompute all denormalised BatchJob counters + status from its BatchItem rows.

    This is the SINGLE SOURCE OF TRUTH for counter/status computation.
    Both ingest (process_job_items) and fan-out (apps/remote/fanout.py) and
    structure-resubmit handlers (views.py, api.py) MUST call this function.

    Canonical definitions
    ---------------------
    completed  = FOUND_LIVE_CACHE + FOUND_SEED_CACHE + FOUND_WISHART_ENTITY
                 + COMPLETE_AFTER_WISHART + INVALID_INPUT + RESULT_KEY_MISMATCH
                 (every item in a terminal state that requires no further action)
    blocking = PENDING_NORMALIZATION + PENDING_CLASSIFICATION + REMOTE_DELAYED
    need_structure = NEED_STRUCTURE  (awaiting external user input)

    Status rules:
    - IN_PROGRESS while any blocking item remains.
    - COMPLETE_WITH_ACTION_REQUIRED when all remaining terminal rows need user
      structure input.
    - COMPLETE when no item requires more work or user action.
    - Status is derived from current item state on every call; if pending items
      appear later, a previously COMPLETE job can return to IN_PROGRESS.
    """
    from django.db.models import Count, Q

    terminal_complete = (
        BatchItemStatus.FOUND_LIVE_CACHE,
        BatchItemStatus.FOUND_SEED_CACHE,
        BatchItemStatus.FOUND_WISHART_ENTITY,
        BatchItemStatus.COMPLETE_AFTER_WISHART,
        BatchItemStatus.INVALID_INPUT,
        BatchItemStatus.RESULT_KEY_MISMATCH,
    )

    blocking_statuses = (
        BatchItemStatus.PENDING_NORMALIZATION,
        BatchItemStatus.PENDING_CLASSIFICATION,
        BatchItemStatus.REMOTE_DELAYED,
    )

    with transaction.atomic():
        job = BatchJob.objects.select_for_update().filter(pk=job_id).first()
        if job is None:
            return

        _reconcile_resolved_classification(job)

        counts = BatchItem.objects.filter(job=job).aggregate(
            total=Count("pk"),
            local_hit=Count("pk", filter=Q(status=BatchItemStatus.FOUND_LIVE_CACHE)),
            seed_hit=Count("pk", filter=Q(status=BatchItemStatus.FOUND_SEED_CACHE)),
            remote_pending=Count(
                "pk",
                filter=Q(
                    status__in=(
                        BatchItemStatus.PENDING_CLASSIFICATION,
                        BatchItemStatus.REMOTE_DELAYED,
                    )
                ),
            ),
            blocking=Count("pk", filter=Q(status__in=blocking_statuses)),
            need_structure=Count("pk", filter=Q(status=BatchItemStatus.NEED_STRUCTURE)),
            invalid=Count("pk", filter=Q(status=BatchItemStatus.INVALID_INPUT)),
            completed=Count("pk", filter=Q(status__in=terminal_complete)),
        )

        total = counts["total"]
        local_hit = counts["local_hit"]
        seed_hit = counts["seed_hit"]
        remote_pending = counts["remote_pending"]
        blocking = counts["blocking"]
        need_structure = counts["need_structure"]
        invalid = counts["invalid"]
        completed = counts["completed"]

        pending_normalization = BatchItem.objects.filter(
            job=job, status=BatchItemStatus.PENDING_NORMALIZATION
        ).exists()

        if blocking:
            new_status = BatchJobStatus.IN_PROGRESS
            completed_at = None
        elif total and need_structure and completed + need_structure == total:
            new_status = BatchJobStatus.COMPLETE_WITH_ACTION_REQUIRED
            completed_at = job.completed_at or timezone.now()
        else:
            new_status = BatchJobStatus.COMPLETE
            completed_at = job.completed_at or timezone.now()

        update_fields: dict[str, object] = {
            "total_count": total,
            "completed_count": completed,
            "local_hit_count": local_hit,
            "seed_hit_count": seed_hit,
            "remote_pending_count": remote_pending,
            "need_structure_count": need_structure,
            "invalid_count": invalid,
            "status": new_status,
            "completed_at": completed_at,
        }

        # Keep the fine-grained processing_state machine in sync with the coarse
        # status, but ONLY once no PENDING_NORMALIZATION items remain — while any
        # row is still un-normalized the durable worker owns processing_state
        # (NORMALIZING/LOCAL_LOOKUP). Once normalization is done this maps the
        # terminal coarse outcome onto the fine-grained terminal state so a
        # cache-hit-only or remote-pending job reaches its resting state even
        # when recompute is driven by fan-out / structure-resubmit rather than
        # the ingest worker.
        if not pending_normalization and job.processing_state != BatchProcessingState.FAILED:
            if new_status == BatchJobStatus.IN_PROGRESS:
                update_fields["processing_state"] = BatchProcessingState.REMOTE_PENDING
            elif new_status == BatchJobStatus.COMPLETE_WITH_ACTION_REQUIRED:
                update_fields["processing_state"] = BatchProcessingState.ACTION_REQUIRED
            else:
                update_fields["processing_state"] = BatchProcessingState.COMPLETE

        BatchJob.objects.filter(pk=job.pk).update(**update_fields)


# Number of items normalized + bulk-looked-up + committed per checkpoint chunk.
PROCESS_CHUNK_SIZE = 1000

_ITEM_UPDATE_FIELDS = [
    "status",
    "detected_type",
    "inchikey",
    "compound",
    "message",
    "original_input",
    "classification_snapshot",
    "resolved_classification",
]


def _process_chunk(items: list[BatchItem], *, remote_enabled: bool) -> None:
    """Normalize + resolve a chunk of items with ONE bulk lookup (no N+1).

    Two passes:
      1. Normalize every item (RDKit; inherently per-row CPU work) and collect
         the 27-char inchikeys that need a cache lookup.
      2. Run a SINGLE ``WHERE inchikey IN (...)`` bulk lookup, then finish each
         item via ``_process_item`` with the pre-fetched result injected so the
         compound-upsert / usage / remote-task / pin semantics are IDENTICAL to
         the legacy per-item path — only the lookup is batched.

    Item rows are updated inside one transaction so the chunk commits atomically
    (a crash mid-chunk leaves those items PENDING_NORMALIZATION → reprocessed).
    """
    # Pass 1: apply the structure-length guard, then normalize ONCE per item
    # (RDKit is expensive — never normalize the same row twice, and never feed
    # RDKit an over-limit string), collecting candidate inchikeys for the bulk
    # lookup. ``norms[i] is None`` marks an over-limit row handled in pass 2.
    norms: list[NormalizedRow | None] = []
    candidate_keys: list[str] = []
    for item in items:
        try:
            check_structure_length(item.original_input)
        except RowTooLongError:
            norms.append(None)
            continue
        norm = normalize_row(item.original_input, external_id=item.external_id)
        norms.append(norm)
        if norm.inchikey and len(norm.inchikey) == 27 and norm.error is None:
            candidate_keys.append(norm.inchikey)

    # Pass 2: ONE bulk lookup for the whole chunk (kills the per-item N+1).
    lookups = lookup_current_classifications(candidate_keys)

    with transaction.atomic():
        for item, chunk_norm in zip(items, norms, strict=True):
            if chunk_norm is None:
                # Over-limit row: drive the legacy guard branch (length check
                # re-raises, truncates original_input + sets INVALID_INPUT).
                item.status = _process_item(item, remote_enabled=remote_enabled)
                continue
            override: object = _NO_LOOKUP_OVERRIDE
            if (
                chunk_norm.inchikey
                and len(chunk_norm.inchikey) == 27
                and chunk_norm.error is None
            ):
                override = lookups.get(chunk_norm.inchikey)
            item.status = _process_item(
                item,
                remote_enabled=remote_enabled,
                lookup_override=override,
                norm_override=chunk_norm,
            )
        BatchItem.objects.bulk_update(items, _ITEM_UPDATE_FIELDS)


def process_job_items(job: BatchJob) -> None:
    """Run local normalization + lookup for all PENDING items in *job*.

    Durable, chunked, restart-safe (F-11):

    * Items are processed in row_order chunks of ``PROCESS_CHUNK_SIZE``. Each
      chunk runs ONE bulk ``WHERE inchikey IN (...)`` lookup + a bulk item update
      (no per-item N+1), committed atomically with a checkpoint advance.
    * The checkpoint (``processed_through_row_order``) lets a crashed/restarted
      worker RESUME: only items still ``PENDING_NORMALIZATION`` are processed, so
      already-resolved rows are never double-processed. The resume frontier is
      exactly the set of PENDING_NORMALIZATION items.
    * Drives the fine-grained ``processing_state`` machine
      (NORMALIZING → LOCAL_LOOKUP → terminal), then ``recompute_job_counters``
      maps it onto the coarse user-facing ``status``.

    This is the SYNCHRONOUS entry point unit tests drive inline; the management
    command claims a job and calls the same path.
    """
    BatchJob.objects.filter(pk=job.pk).update(
        processing_state=BatchProcessingState.NORMALIZING
    )

    while True:
        chunk = list(
            BatchItem.objects.filter(
                job=job, status=BatchItemStatus.PENDING_NORMALIZATION
            )
            .order_by("row_order")[:PROCESS_CHUNK_SIZE]
        )
        if not chunk:
            break

        BatchJob.objects.filter(pk=job.pk).update(
            processing_state=BatchProcessingState.LOCAL_LOOKUP
        )
        _process_chunk(chunk, remote_enabled=job.remote_enabled)

        # Checkpoint: advance the cursor to the highest row_order in this chunk.
        # A restarted worker re-queries PENDING_NORMALIZATION (the true resume
        # frontier); the cursor is the durable, language-agnostic progress marker.
        last_row_order = chunk[-1].row_order
        BatchJob.objects.filter(pk=job.pk).update(
            processed_through_row_order=last_row_order
        )

    recompute_job_counters(job.pk)
    # Refresh in-memory
    job.refresh_from_db()


# ---------------------------------------------------------------------------
# Durable claim (SELECT ... FOR UPDATE SKIP LOCKED) for the ingest worker
# ---------------------------------------------------------------------------


def claim_job_for_processing(worker_id: str) -> BatchJob | None:
    """Atomically claim the next job needing ingest processing (SKIP LOCKED).

    Mirrors the remote queue protocol: a ``SELECT ... FOR UPDATE SKIP LOCKED``
    over jobs in a claimable processing_state, flipping it to PARSING with a
    fresh lease so any-language worker can drive the same column. Returns the
    claimed job, or None when nothing needs work.
    """
    with transaction.atomic():
        job = (
            BatchJob.objects.select_for_update(skip_locked=True)
            .filter(processing_state__in=CLAIMABLE_PROCESSING_STATES)
            .order_by("created_at", "id")
            .first()
        )
        if job is None:
            return None
        job.processing_state = BatchProcessingState.PARSING
        job.claimed_by = worker_id
        job.claimed_at = timezone.now()
        job.claim_lease_version = job.claim_lease_version + 1
        job.save(
            update_fields=[
                "processing_state",
                "claimed_by",
                "claimed_at",
                "claim_lease_version",
            ]
        )
        return job


def process_claimed_job(job: BatchJob) -> None:
    """Run the durable processing pipeline for an already-claimed job.

    Idempotent + restart-safe: delegates to the checkpointed
    :func:`process_job_items`, which only touches PENDING_NORMALIZATION items, so
    re-running a partially-processed job never double-processes. On error the job
    is parked in FAILED with the reason recorded; the items it already resolved
    stay resolved.
    """
    try:
        process_job_items(job)
    except Exception as exc:
        BatchJob.objects.filter(pk=job.pk).update(
            processing_state=BatchProcessingState.FAILED,
            status=BatchJobStatus.FAILED,
            processing_error=str(exc)[:2000],
        )
        raise


def run_pending_jobs(worker_id: str, *, max_jobs: int | None = None) -> int:
    """Claim + process pending jobs until the queue is drained (or *max_jobs*).

    Returns the number of jobs processed. Used by the ``process_jobs``
    management command and by tests that want to drive the durable path.
    """
    processed = 0
    while max_jobs is None or processed < max_jobs:
        job = claim_job_for_processing(worker_id)
        if job is None:
            break
        process_claimed_job(job)
        processed += 1
    return processed


def create_job_records(
    rows: Iterable[dict],
    *,
    remote_enabled: bool = True,
) -> tuple[BatchJob, str]:
    """Create a BatchJob and ingest its raw rows without local processing.

    Parameters
    ----------
    rows:
        Iterable of parser-style dicts (keys: raw, row_index, external_id?).
    remote_enabled:
        Whether remote classification tasks may be created.

    Returns
    -------
    (job, plaintext_secret)
        The created job and the one-time plaintext secret.  The caller must
        embed the secret in the response; it is never stored.
    """
    plaintext_secret, secret_hash = _generate_secret()

    with transaction.atomic():
        job = BatchJob.objects.create(
            secret_hash=secret_hash,
            status=BatchJobStatus.PENDING,
            remote_enabled=remote_enabled,
            # PARSING is a claimable state: the durable worker can pick the job
            # up if the synchronous path does not (or has not yet) processed it.
            processing_state=BatchProcessingState.PARSING,
        )
        ingest_rows(job, rows)

    return job, plaintext_secret


def create_job(
    rows: Iterable[dict],
    *,
    remote_enabled: bool = True,
) -> tuple[BatchJob, str]:
    """Create a BatchJob, ingest rows, and run local processing.

    The durable job row and raw items are committed before local processing so
    a processing error cannot roll back the existence of the job itself.
    """
    job, plaintext_secret = create_job_records(
        rows,
        remote_enabled=remote_enabled,
    )

    # Local processing runs outside the creation transaction so that a partial
    # error in one item does not roll back the job row itself.
    process_job_items(job)

    return job, plaintext_secret
