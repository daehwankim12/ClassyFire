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
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import cast

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.batches.ingest import (
    ingest_rows,
    iter_uploaded_records_from_path,
    validate_upload_root,
)
from apps.batches.models import BatchItem, BatchJob
from apps.batches.remote_delay import REMOTE_DISABLED_MESSAGE
from apps.classifications.services import (
    ClassificationLookupResult,
    LookupTier,
    classification_lookup_snapshot,
    lookup_current_classification,
    lookup_current_classifications,
)
from apps.compounds.inchikey import is_standard_inchikey, same_inchikey_skeleton
from apps.compounds.models import CompoundStructure, CompoundUsage
from apps.compounds.validators import NormalizedRow, normalize_row

# Shown for a bare NON-STANDARD InChIKey input: it cannot be matched against the
# standard-keyed caches directly (InChIKey is a one-way hash), but supplying the
# structure lets us re-index it under its standard key (see
# submit_structure_for_item, which adopts the structure's standard key).
NON_STANDARD_INCHIKEY_MESSAGE = (
    "Non-standard InChIKey: cannot match standard caches directly. Provide the "
    "structure (SMILES/InChI) or a standard InChIKey — a supplied structure is "
    "re-indexed under its standard key."
)
from apps.core.enums import (
    CLAIMABLE_PROCESSING_STATES,
    BatchItemStatus,
    BatchJobStatus,
    BatchProcessingState,
    DetectedInputType,
)
from apps.core.security import MAX_STRUCTURE_LEN, RowTooLongError, check_structure_length
from apps.remote import queue as remote_queue
from apps.remote.budget import RemoteBudgetExceededError, normalize_principal
from apps.remote.models import RemoteTask
from apps.remote.queue import RemoteQueueBackpressure

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
    *,
    principal: str | None = None,
) -> RemoteTask:
    """Get or create an active RemoteTask for *inchikey* (global dedupe).

    The partial-unique constraint allows only ONE active task per inchikey.
    We try to create; on IntegrityError (duplicate active task) we fetch
    the existing one in a savepoint so the outer transaction is unaffected.

    If the global queue is at its high-water depth, ``enqueue_task`` raises
    :class:`RemoteQueueBackpressure`; that propagates out of the savepoint (it is
    NOT an IntegrityError) so the caller can park the item as REMOTE_DELAYED. The
    web/API ingest call sites catch it — it never surfaces as a 500 (H-05).
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
            task = remote_queue.enqueue_task(
                inchikey=inchikey,
                compound_id=compound.pk if compound is not None else None,
                principal=principal,
            )
            if task is None:
                raise IntegrityError("remote task create race")
            return task
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
            # A NON-STANDARD stored key can never equal the structure's recomputed
            # STANDARD key, but the supplied structure is authoritative: adopt its
            # standard key when the connectivity skeleton (first block) matches —
            # a guard against pasting an unrelated structure. A standard stored
            # key must still match exactly.
            adopt_standard = not is_standard_inchikey(item.inchikey) and (
                same_inchikey_skeleton(item.inchikey, norm.inchikey)
            )
            if not adopt_standard:
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
                try:
                    _get_or_create_remote_task(
                        resolved_inchikey,
                        compound,
                        principal=job.remote_principal,
                    )
                except RemoteBudgetExceededError:
                    item.status = BatchItemStatus.REMOTE_DELAYED
                    item.message = "Remote budget exceeded for this principal."
                    item.save(update_fields=["status", "message"])
                except RemoteQueueBackpressure:
                    item.status = BatchItemStatus.REMOTE_DELAYED
                    item.message = _REMOTE_QUEUE_BACKPRESSURE_MESSAGE
                    item.save(update_fields=["status", "message"])
            else:
                item.status = BatchItemStatus.REMOTE_DELAYED
                item.message = REMOTE_DISABLED_MESSAGE
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


def _submitted_type_override(item: BatchItem) -> DetectedInputType | None:
    """Return a DetectedInputType override from item.submitted_type, or None."""
    if item.submitted_type in DetectedInputType.values:
        return DetectedInputType(item.submitted_type)
    return None


def _process_item(
    item: BatchItem,
    *,
    remote_enabled: bool,
    remote_principal: str = "system",
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

    # Pre-Step: Reject rows flagged by the parser as overlong without sending
    # them to RDKit.  This fires for both the legacy single-item path and the
    # prepared chunk path (via the norm=None branch in _prepare_chunk).
    if item.parse_error:
        total = item.overflow_length or len(item.original_input)
        item.original_input = (
            f"{item.original_input[:200]}[truncated, {total} chars total]"
        )
        item.status = BatchItemStatus.INVALID_INPUT
        item.message = (
            f"Row exceeds maximum length ({total} chars; limit {MAX_STRUCTURE_LEN})"
        )
        return BatchItemStatus.INVALID_INPUT

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
        norm = normalize_row(
            item.original_input,
            type_override=_submitted_type_override(item),
            external_id=item.external_id,
        )
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

        # A bare NON-STANDARD InChIKey can't match standard-keyed caches; flag it
        # so the eventual NEED_STRUCTURE state is explained (a supplied structure
        # is later re-indexed under its standard key).
        if not has_structure and not is_standard_inchikey(norm.inchikey):
            item.message = NON_STANDARD_INCHIKEY_MESSAGE

        if not remote_enabled:
            if has_structure:
                item.message = REMOTE_DISABLED_MESSAGE
            return (
                BatchItemStatus.REMOTE_DELAYED
                if has_structure
                else BatchItemStatus.NEED_STRUCTURE
            )

        # Create/reuse a RemoteTask. Key-only tasks are entity lookups; structure
        # tasks can proceed to calculation if entity lookup misses.
        try:
            _get_or_create_remote_task(
                norm.inchikey,
                compound,
                principal=remote_principal,
            )
        except RemoteBudgetExceededError:
            item.message = "Remote budget exceeded for this principal."
            return BatchItemStatus.REMOTE_DELAYED
        except RemoteQueueBackpressure:
            item.message = _REMOTE_QUEUE_BACKPRESSURE_MESSAGE
            return BatchItemStatus.REMOTE_DELAYED
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
    from django.db.models import Count

    terminal_complete = (
        BatchItemStatus.FOUND_LIVE_CACHE,
        BatchItemStatus.FOUND_SEED_CACHE,
        BatchItemStatus.FOUND_WISHART_ENTITY,
        BatchItemStatus.COMPLETE_AFTER_WISHART,
        BatchItemStatus.INVALID_INPUT,
        BatchItemStatus.RESULT_KEY_MISMATCH,
        # Remote give-up: a timed-out item requires no further work, so it counts
        # toward "completed" and lets the job leave IN_PROGRESS (it renders as a
        # non-FOUND row, i.e. "Failed", in exports — see exports._build_row_dict).
        BatchItemStatus.REMOTE_TIMED_OUT,
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


@dataclass(frozen=True)
class IngestJobClaim:
    """Immutable snapshot of an ingest worker's BatchJob claim."""

    job_id: uuid.UUID
    worker_id: str
    claim_lease_version: int


@dataclass(frozen=True)
class PreparedItem:
    """A normalized chunk item ready for claim-fenced DB side effects."""

    item_id: int
    norm: NormalizedRow | None
    lookup: object


def claim_from_job(job: BatchJob) -> IngestJobClaim:
    """Snapshot a job's CURRENT ingest claim fields."""
    return IngestJobClaim(
        job_id=job.pk,
        worker_id=str(job.claimed_by or ""),
        claim_lease_version=job.claim_lease_version,
    )


def _claim_filter_kwargs(claim: IngestJobClaim) -> dict[str, object]:
    filters: dict[str, object] = {
        "pk": claim.job_id,
        "claim_lease_version": claim.claim_lease_version,
    }
    if claim.worker_id:
        filters["claimed_by"] = claim.worker_id
    else:
        filters["claimed_by__isnull"] = True
    return filters


def _lock_owned_job(claim: IngestJobClaim) -> BatchJob:
    """Lock the claimed BatchJob row and verify the captured claim still owns it."""
    locked = BatchJob.objects.select_for_update().get(pk=claim.job_id)
    if (
        str(locked.claimed_by or "") != claim.worker_id
        or locked.claim_lease_version != claim.claim_lease_version
    ):
        raise IngestLeaseLost(claim.job_id)
    return locked


def _fenced_claim_update(claim: IngestJobClaim, **fields: object) -> None:
    updated = BatchJob.objects.filter(**_claim_filter_kwargs(claim)).update(**fields)
    if updated != 1:
        raise IngestLeaseLost(claim.job_id)


def _prepare_chunk(items: list[BatchItem]) -> list[PreparedItem]:
    """Normalize a chunk and run one bulk lookup without writing DB side effects.

    Two passes:
      1. Normalize every item (RDKit; inherently per-row CPU work) and collect
         the 27-char inchikeys that need a cache lookup.
      2. Run a SINGLE ``WHERE inchikey IN (...)`` bulk lookup, then finish each
         item via ``_process_item`` with the pre-fetched result injected so the
         compound-upsert / usage / remote-task / pin semantics are IDENTICAL to
         the legacy per-item path — only the lookup is batched.

    This function prepares data only. Compound usage, remote queue/budget,
    item updates, and checkpoint writes happen later in ``_commit_prepared_chunk``
    after the worker claim has been locked and verified.
    """
    # Pass 1: apply the structure-length guard, then normalize ONCE per item
    # (RDKit is expensive — never normalize the same row twice, and never feed
    # RDKit an over-limit string), collecting candidate inchikeys for the bulk
    # lookup. ``norms[i] is None`` marks an over-limit row handled in pass 2.
    norms: dict[int, NormalizedRow | None] = {}
    candidate_keys: list[str] = []
    for item in items:
        # Parser-flagged overlong rows: route to _process_item without
        # normalization (mirrors the RowTooLongError branch below).
        if item.parse_error:
            norms[item.id] = None
            continue
        try:
            check_structure_length(item.original_input)
        except RowTooLongError:
            norms[item.id] = None
            continue
        norm = normalize_row(
            item.original_input,
            type_override=_submitted_type_override(item),
            external_id=item.external_id,
        )
        norms[item.id] = norm
        if norm.inchikey and len(norm.inchikey) == 27 and norm.error is None:
            candidate_keys.append(norm.inchikey)

    # Pass 2: ONE bulk lookup for the whole chunk (kills the per-item N+1).
    lookups = lookup_current_classifications(candidate_keys)

    prepared: list[PreparedItem] = []
    for item in items:
        row_norm = norms[item.id]
        override: object = _NO_LOOKUP_OVERRIDE
        if (
            row_norm
            and row_norm.inchikey
            and len(row_norm.inchikey) == 27
            and row_norm.error is None
        ):
            override = lookups.get(row_norm.inchikey)
        prepared.append(PreparedItem(item_id=item.id, norm=row_norm, lookup=override))
    return prepared


def _commit_prepared_chunk(
    claim: IngestJobClaim,
    prepared: list[PreparedItem],
    *,
    remote_enabled: bool,
    remote_principal: str = "system",
    checkpoint_row_order: int,
) -> None:
    """Commit a prepared chunk only while holding the captured job claim."""
    with transaction.atomic():
        locked_job = _lock_owned_job(claim)
        items = list(
            BatchItem.objects.select_for_update()
            .filter(
                job=locked_job,
                id__in=[item.item_id for item in prepared],
                status=BatchItemStatus.PENDING_NORMALIZATION,
            )
            .order_by("row_order")
        )
        prepared_by_id = {item.item_id: item for item in prepared}

        for item in items:
            entry = prepared_by_id[item.id]
            if entry.norm is None:
                # Over-limit row: drive the legacy guard branch (length check
                # re-raises, truncates original_input + sets INVALID_INPUT).
                item.status = _process_item(
                    item,
                    remote_enabled=remote_enabled,
                    remote_principal=remote_principal,
                )
                continue
            item.status = _process_item(
                item,
                remote_enabled=remote_enabled,
                remote_principal=remote_principal,
                lookup_override=entry.lookup,
                norm_override=entry.norm,
            )
        BatchItem.objects.bulk_update(items, _ITEM_UPDATE_FIELDS)
        locked_job.processing_state = BatchProcessingState.LOCAL_LOOKUP
        locked_job.processed_through_row_order = checkpoint_row_order
        locked_job.claimed_at = timezone.now()
        locked_job.save(
            update_fields=[
                "processing_state",
                "processed_through_row_order",
                "claimed_at",
            ]
        )


_REMOTE_QUEUE_BACKPRESSURE_MESSAGE = "Remote queue is at capacity; retrying later."


def _retry_backpressured_remote_delayed_items(
    claim: IngestJobClaim,
    *,
    remote_enabled: bool,
    remote_principal: str = "system",
) -> None:
    """Retry durable enqueue for items delayed only by remote queue capacity.

    Backpressure happens before a RemoteTask can be inserted, so the BatchItem is
    the durable retry record.  Once a REMOTE_PENDING job is reclaimed by the
    ingest worker, retry only rows carrying the capacity message; budget-denied,
    remote-disabled, and other REMOTE_DELAYED rows remain user/action-gated.
    """
    if not remote_enabled:
        return

    with transaction.atomic():
        # Lock retryable item rows before the parent job row. Remote fan-out
        # updates matching BatchItem rows and then recomputes counters, which
        # locks the BatchJob row; taking locks in that same item -> job order
        # avoids a PostgreSQL ABBA deadlock for the same InChIKey.
        items = list(
            BatchItem.objects.select_for_update()
            .filter(
                job_id=claim.job_id,
                status=BatchItemStatus.REMOTE_DELAYED,
                message=_REMOTE_QUEUE_BACKPRESSURE_MESSAGE,
                inchikey__isnull=False,
            )
            .order_by("row_order")
        )
        _lock_owned_job(claim)
        for item in items:
            inchikey = cast(str, item.inchikey)
            result = lookup_current_classification(inchikey)
            if result is not None:
                item.classification_snapshot = classification_lookup_snapshot(result)
                _pin_resolved_classification(item, item.classification_snapshot)
                item.status = (
                    BatchItemStatus.FOUND_LIVE_CACHE
                    if result.tier == LookupTier.LIVE
                    else BatchItemStatus.FOUND_SEED_CACHE
                )
                item.message = None
                item.save(update_fields=_ITEM_UPDATE_FIELDS)
                continue

            try:
                _get_or_create_remote_task(
                    inchikey,
                    item.compound,
                    principal=remote_principal,
                )
            except RemoteBudgetExceededError:
                item.message = "Remote budget exceeded for this principal."
                item.save(update_fields=["message"])
            except RemoteQueueBackpressure:
                # Capacity is still exhausted. Leave the item as the durable
                # retry record for a later ingest-worker pass.
                continue
            else:
                item.status = BatchItemStatus.PENDING_CLASSIFICATION
                item.message = None
                item.save(update_fields=["status", "message"])


class IngestLeaseLost(Exception):  # noqa: N818 - mirrors remote LeaseLost contract
    """Raised when a fenced ingest write hits 0 rows.

    A worker captures its claim owner + lease version on the in-memory ``job``
    handle at claim time. If another worker reclaims the job (bumping
    ``claim_lease_version``), every subsequent fenced write from the stale worker
    matches 0 rows and raises this, so a reclaimed/crashed worker can never keep
    writing over the new owner's state.
    """

    def __init__(self, job_pk: object) -> None:
        super().__init__(f"ingest lease lost for batch job {job_pk}")
        self.job_pk = job_pk


def _fenced_job_update(job: BatchJob, **fields: object) -> None:
    """Apply a job write fenced to the captured claim owner + lease version.

    Mirrors the remote-queue lease/version fencing: the update only lands if the
    DB row still carries the same ``claimed_by`` and ``claim_lease_version`` the
    worker held at claim time. A stale worker (one whose job was reclaimed) hits
    0 rows and raises :class:`IngestLeaseLost`.
    """
    _fenced_claim_update(claim_from_job(job), **fields)


class QuotaExceededError(Exception):
    """A spooled upload exceeded a row-count quota at worker staging time.

    The job is parked terminal-FAILED with this reason (mirroring the old
    in-request rollback-to-quota-error behavior) rather than producing items.
    """


def _resolve_spooled_artifact(job: BatchJob) -> Path:
    """Resolve a job's spooled artifact SAFELY under UPLOAD_ARTIFACT_DIR.

    Rejects any ``upload_artifact_path`` that escapes the configured root
    (path traversal / absolute path), so a tampered DB value can never make the
    worker read an arbitrary file.
    """
    root = validate_upload_root(getattr(settings, "UPLOAD_ARTIFACT_DIR", ""))
    artifact = (root / (job.upload_artifact_path or "")).resolve()
    try:
        artifact.relative_to(root)
    except ValueError as exc:
        raise ValueError("upload artifact path escapes upload root") from exc
    return artifact


def stage_spooled_upload(job: BatchJob, *, claim: IngestJobClaim | None = None) -> None:
    """Parse a spooled upload artifact into BatchItems (UPLOADING → PARSING).

    Deferred-parse counterpart of the old in-request staging (H-04). Resolves the
    artifact safely, streams its rows into BatchItems via ``ingest_rows`` with the
    per-job ``MAX_ROWS_PER_JOB`` hard cap, enforces the daily row cap that the
    in-request path used to enforce, then advances the claim-fenced
    processing_state to PARSING so normalization proceeds.

    A row-count-quota violation rolls the staged rows back and raises
    :class:`QuotaExceededError`; the caller parks the job terminal-FAILED.

    Claim-fenced (H-01): the exists-check, ``ingest_rows``, the daily-cap query,
    the quota event, and the ``UPLOADING → PARSING`` state transition ALL run
    inside ONE ``transaction.atomic()`` that FIRST locks + verifies the captured
    claim via :func:`_lock_owned_job`. A stale worker (its job reclaimed, lease
    version bumped) raises :class:`IngestLeaseLost` BEFORE any write, so it can
    never commit rows/quota for a job it no longer owns; concurrent stagers
    serialize on the locked job row, so the ``exists()`` race that produced
    duplicate rows / ``(job, row_order)`` collisions is closed.

    Idempotent: a job whose artifact was already staged (items exist) is skipped,
    so a worker that crashed after committing rows but before finishing
    normalization re-enters here, locks the row, sees items exist, and returns
    (state already PARSING because rows + state committed atomically).
    """
    if not job.upload_artifact_path:
        return

    claim = claim or claim_from_job(job)
    artifact = _resolve_spooled_artifact(job)

    max_rows = int(getattr(settings, "MAX_ROWS_PER_JOB", 10_000))
    daily_cap = int(getattr(settings, "JOB_CREATE_DAILY_ROW_CAP", 100_000))
    day_ago = timezone.now() - timedelta(days=1)

    from apps.batches.models import BatchQuotaEvent
    from apps.core.security import _acquire_quota_advisory_lock

    try:
        with transaction.atomic():
            # Lock + verify the claim FIRST: a stale worker (job reclaimed)
            # raises IngestLeaseLost here, before any row/quota write, and
            # concurrent stagers serialize on this locked row.
            locked_job = _lock_owned_job(claim)

            # Re-check under the lock: a concurrent stager that won the race
            # already committed rows + flipped state, so this worker returns
            # without double-staging.
            if BatchItem.objects.filter(job_id=locked_job.pk).exists():
                return

            row_iter = iter_uploaded_records_from_path(
                artifact,
                original_name=locked_job.upload_original_name or artifact.name,
                content_type=locked_job.upload_content_type or "",
            )
            row_count = ingest_rows(locked_job, row_iter, max_rows=max_rows)

            # H-05: serialize the daily-cap critical section PER PRINCIPAL.
            # Two workers staging two DIFFERENT jobs for the SAME principal lock
            # different job rows via _lock_owned_job (no contention), so without
            # this both would read the SAME used_rows, both pass the cap check,
            # and both commit -> the cap is exceeded. Acquire a per-principal
            # transaction-scoped advisory lock here, AFTER the slow ingest_rows
            # parse (which stays concurrent across workers) and JUST BEFORE the
            # used_rows read, so only the fast cap-check + KIND_JOB_ROWS insert
            # is serialized. The pg xact lock auto-releases at commit/rollback.
            #
            # Lock ordering: this worker takes the job-row lock (_lock_owned_job)
            # THEN this advisory lock. The request path takes the advisory lock
            # THEN INSERTs a NEW BatchJob (it never SELECT...FOR UPDATEs an
            # existing job row while holding the advisory lock), so there is no
            # opposite-order holder -> no deadlock. Two same-principal workers
            # contend only on this advisory key; the holder never waits on the
            # other's job row -> no circular wait. After acquiring the lock the
            # second worker re-reads used_rows and now SEES the first worker's
            # committed KIND_JOB_ROWS, so the cap is enforced correctly.
            _acquire_quota_advisory_lock(
                BatchQuotaEvent.KIND_JOB_CREATE,
                f"principal:{locked_job.remote_principal}",
                "",
            )

            # Daily row cap: re-check against the principal's last-24h usage now
            # that the real row count is known (the in-request quota event was
            # recorded with row_count=0). Roll back the staged rows if exceeded.
            used_rows = (
                BatchQuotaEvent.objects.filter(
                    kind__in=[
                        BatchQuotaEvent.KIND_JOB_CREATE,
                        BatchQuotaEvent.KIND_JOB_ROWS,
                    ],
                    created_at__gte=day_ago,
                    principal=locked_job.remote_principal,
                ).aggregate(total=Sum("row_count"))["total"]
                or 0
            )
            if used_rows + row_count > daily_cap:
                transaction.set_rollback(True)
                raise QuotaExceededError("Daily row quota exceeded.")

            BatchQuotaEvent.objects.create(
                kind=BatchQuotaEvent.KIND_JOB_ROWS,
                principal=locked_job.remote_principal,
                ip_address="",
                row_count=row_count,
            )

            # Flip UPLOADING → PARSING in the SAME transaction as the rows +
            # quota event, so a worker that re-enters after a committed stage
            # sees both items and the advanced state atomically.
            locked_job.processing_state = BatchProcessingState.PARSING
            locked_job.claimed_at = timezone.now()
            locked_job.save(update_fields=["processing_state", "claimed_at"])
    except RowCapExceededError as exc:
        raise QuotaExceededError(
            f"Job has more than {exc.max_rows} rows; the maximum is {exc.max_rows}."
        ) from exc

    job.processing_state = BatchProcessingState.PARSING


def process_job_items(job: BatchJob, *, claim: IngestJobClaim | None = None) -> None:
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
    claim = claim or claim_from_job(job)

    # H-04: a job with a spooled upload artifact but no BatchItems yet still
    # needs its raw upload parsed. Parse the artifact into rows BEFORE
    # normalization. This runs inside the durable worker, so the bounded
    # streaming parse + RDKit-free row staging is OUT of the HTTP request. The
    # claim flips processing_state UPLOADING→PARSING, so gate on the artifact +
    # items-not-yet-staged rather than on the (already-flipped) state.
    if job.upload_artifact_path:
        stage_spooled_upload(job, claim=claim)

    # Fenced to the claim owner+version: a reclaimed (version-bumped) job makes
    # this FIRST write hit 0 rows and raise IngestLeaseLost before any chunk
    # work runs.
    _fenced_claim_update(
        claim,
        processing_state=BatchProcessingState.NORMALIZING,
        claimed_at=timezone.now(),
    )

    while True:
        _fenced_claim_update(claim, claimed_at=timezone.now())
        chunk = list(
            BatchItem.objects.filter(
                job_id=claim.job_id, status=BatchItemStatus.PENDING_NORMALIZATION
            )
            .order_by("row_order")[:PROCESS_CHUNK_SIZE]
        )
        if not chunk:
            break

        prepared = _prepare_chunk(chunk)
        _commit_prepared_chunk(
            claim,
            prepared,
            remote_enabled=job.remote_enabled,
            remote_principal=job.remote_principal,
            checkpoint_row_order=chunk[-1].row_order,
        )

    _retry_backpressured_remote_delayed_items(
        claim,
        remote_enabled=job.remote_enabled,
        remote_principal=job.remote_principal,
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
    stale_before = timezone.now() - timedelta(
        seconds=getattr(settings, "INGEST_CLAIM_LEASE_SECONDS", 900)
    )
    with transaction.atomic():
        # Claim only never-claimed jobs (claimed_by is null) or jobs whose lease
        # has gone stale (claimed_at older than the TTL). An actively-leased job
        # is excluded, so a job being driven by another worker cannot be stolen.
        job = (
            BatchJob.objects.select_for_update(skip_locked=True)
            .filter(processing_state__in=CLAIMABLE_PROCESSING_STATES)
            .filter(Q(claimed_by__isnull=True) | Q(claimed_at__lt=stale_before))
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
    claim = claim_from_job(job)
    try:
        process_job_items(job, claim=claim)
    except IngestLeaseLost:
        # The lease was lost mid-run: another worker now owns this job. Do NOT
        # park it FAILED - that would clobber the new owner's state. Propagate so
        # the caller drops this stale handle.
        raise
    except QuotaExceededError as exc:
        # A spooled upload exceeded a row-count cap at worker staging time (H-04
        # deferred enforcement). This is an EXPECTED terminal outcome, not a
        # worker crash: park the job terminal-FAILED (claim-fenced) and return
        # cleanly so the worker loop keeps draining other jobs.
        _fenced_claim_update(
            claim,
            processing_state=BatchProcessingState.FAILED,
            status=BatchJobStatus.FAILED,
            processing_error=str(exc)[:2000],
        )
        return
    except Exception as exc:
        # Fence the FAILED parking write too: a worker that lost its lease while
        # raising some OTHER error must not stomp the new owner's state. If this
        # fenced write itself finds the lease gone, let IngestLeaseLost propagate.
        _fenced_claim_update(
            claim,
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
    remote_principal: str = "system",
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
            remote_principal=normalize_principal(remote_principal),
            # PARSING is a claimable state: the durable worker can pick the job
            # up if the synchronous path does not (or has not yet) processed it.
            processing_state=BatchProcessingState.PARSING,
        )
        ingest_rows(job, rows)

    return job, plaintext_secret


class RowCapExceededError(ValueError):
    """The streamed upload exceeded the per-job row cap mid-stream."""

    def __init__(self, max_rows: int) -> None:
        self.max_rows = max_rows
        super().__init__(f"Job exceeds the maximum of {max_rows} rows.")


def create_job_records_streaming(
    rows: Iterable[dict],
    *,
    remote_enabled: bool = True,
    remote_principal: str = "system",
    max_rows: int | None = None,
) -> tuple[BatchJob, str, int]:
    """Create a BatchJob and STREAM-ingest its rows, returning the row count.

    For the deferred (HTTP-returns-immediately) path: the parser iterator is
    consumed lazily into bounded ``bulk_create`` chunks — the whole row list is
    never materialised. If ``max_rows`` is given and the stream exceeds it, the
    transaction is rolled back and :class:`RowCapExceededError` is raised so the route
    can return 413 without persisting a partial job.

    Returns ``(job, plaintext_secret, row_count)``.
    """
    plaintext_secret, secret_hash = _generate_secret()

    with transaction.atomic():
        job = BatchJob.objects.create(
            secret_hash=secret_hash,
            status=BatchJobStatus.PENDING,
            remote_enabled=remote_enabled,
            remote_principal=normalize_principal(remote_principal),
            processing_state=BatchProcessingState.PARSING,
        )
        row_count = ingest_rows(job, rows, max_rows=max_rows)

    return job, plaintext_secret, row_count


def create_job(
    rows: Iterable[dict],
    *,
    remote_enabled: bool = True,
    remote_principal: str = "system",
) -> tuple[BatchJob, str]:
    """Create a BatchJob, ingest rows, and run local processing.

    The durable job row and raw items are committed before local processing so
    a processing error cannot roll back the existence of the job itself.
    """
    job, plaintext_secret = create_job_records(
        rows,
        remote_enabled=remote_enabled,
        remote_principal=remote_principal,
    )

    # Local processing runs outside the creation transaction so that a partial
    # error in one item does not roll back the job row itself.
    process_job_items(job)

    return job, plaintext_secret
