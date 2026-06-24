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

from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.batches.ingest import ingest_rows
from apps.batches.models import BatchItem, BatchJob
from apps.classifications.services import (
    LookupTier,
    classification_lookup_snapshot,
    lookup_current_classification,
)
from apps.compounds.models import CompoundStructure, CompoundUsage
from apps.compounds.validators import normalize_row
from apps.core.enums import (
    BatchItemStatus,
    BatchJobStatus,
    DetectedInputType,
    TaskState,
)
from apps.core.security import MAX_STRUCTURE_LEN, RowTooLongError, check_structure_length
from apps.remote.models import RemoteTask

# Number of characters preserved in the bounded preview for an over-limit row.
_OVERLONG_PREVIEW_CHARS = 200


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

    with transaction.atomic():
        compound = (
            CompoundStructure.objects.select_for_update()
            .filter(inchikey=inchikey)
            .order_by("created_at", "pk")
            .first()
        )
        if compound is None:
            return CompoundStructure.objects.create(
                inchikey=inchikey,
                original_input=original_input,
                original_smiles=canonical_smiles,
                canonical_smiles=canonical_smiles,
                inchi=inchi,
            )

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
            RemoteTask.objects.filter(pk=existing.pk, compound__isnull=True).update(
                compound=compound
            )
            existing.refresh_from_db(fields=["compound"])
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
        return RemoteTask.objects.filter(
            inchikey=inchikey,
            state__in=ACTIVE_TASK_STATES,
        ).get()


def _process_item(item: BatchItem, *, remote_enabled: bool) -> BatchItemStatus:
    """Normalize + local lookup for a single item.  Returns the final status.

    Updates item fields (inchikey, detected_type, compound, message) in place;
    the caller is responsible for calling item.save().
    """
    item.classification_snapshot = None

    # Step 0: Guard — reject rows whose structure string exceeds MAX_STRUCTURE_LEN.
    # This check lives here (service layer) so it covers BOTH the UI and API paths.
    # For an over-limit row we preserve a bounded preview of the original input
    # (FR-004) rather than a synthetic sentinel, and record an explicit message.
    try:
        check_structure_length(item.original_input)
    except RowTooLongError:
        raw_len = len(item.original_input)
        preview = item.original_input[:_OVERLONG_PREVIEW_CHARS]
        item.original_input = f"{preview}…[truncated, {raw_len} chars total]"
        item.message = (
            f"Row exceeds maximum length ({raw_len} chars; limit {MAX_STRUCTURE_LEN})"
        )
        return BatchItemStatus.INVALID_INPUT

    # Step 1: Normalize
    norm = normalize_row(item.original_input, external_id=item.external_id)
    item.detected_type = norm.detected_type

    # Step 2: Handle normalization error
    if norm.error:
        item.message = norm.error
        return BatchItemStatus.INVALID_INPUT

    # Step 3: Lookup (only possible with a 27-char inchikey)
    if norm.inchikey and len(norm.inchikey) == 27:
        item.inchikey = norm.inchikey
        result = lookup_current_classification(norm.inchikey)
        if result is not None:
            item.classification_snapshot = classification_lookup_snapshot(result)
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
    remote_pending = PENDING_CLASSIFICATION + REMOTE_DELAYED
    need_structure = NEED_STRUCTURE  (awaiting external user input)

    Status rules:
    - COMPLETE when remote_pending == 0  (no in-flight remote work remains)
    - NEED_STRUCTURE items do NOT block COMPLETE; they await user input and are
      shown separately via need_structure_count.
    - A job that is already COMPLETE is never regressed back to IN_PROGRESS.
    """
    from django.db.models import Count, Q

    job = BatchJob.objects.filter(pk=job_id).first()
    if job is None:
        return

    terminal_complete = (
        BatchItemStatus.FOUND_LIVE_CACHE,
        BatchItemStatus.FOUND_SEED_CACHE,
        BatchItemStatus.FOUND_WISHART_ENTITY,
        BatchItemStatus.COMPLETE_AFTER_WISHART,
        BatchItemStatus.INVALID_INPUT,
        BatchItemStatus.RESULT_KEY_MISMATCH,
    )

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
        need_structure=Count("pk", filter=Q(status=BatchItemStatus.NEED_STRUCTURE)),
        invalid=Count("pk", filter=Q(status=BatchItemStatus.INVALID_INPUT)),
        completed=Count("pk", filter=Q(status__in=terminal_complete)),
    )

    total = counts["total"]
    local_hit = counts["local_hit"]
    seed_hit = counts["seed_hit"]
    remote_pending = counts["remote_pending"]
    need_structure = counts["need_structure"]
    invalid = counts["invalid"]
    completed = counts["completed"]

    # Status: COMPLETE when no item is in-flight (remote_pending == 0).
    # NEED_STRUCTURE items await user input — they don't block COMPLETE.
    # The status is always derived from ground-truth item counts; this function
    # is the authoritative recompute so we do NOT apply a "never regress" guard
    # here — if the DB shows pending items, the job IS in-progress regardless
    # of its previous status.
    from datetime import datetime as _datetime

    completed_at: _datetime | None
    if remote_pending == 0:
        new_status = BatchJobStatus.COMPLETE
        completed_at = job.completed_at if job.completed_at is not None else timezone.now()
    else:
        new_status = BatchJobStatus.IN_PROGRESS
        completed_at = job.completed_at

    BatchJob.objects.filter(pk=job.pk).update(
        total_count=total,
        completed_count=completed,
        local_hit_count=local_hit,
        seed_hit_count=seed_hit,
        remote_pending_count=remote_pending,
        need_structure_count=need_structure,
        invalid_count=invalid,
        status=new_status,
        completed_at=completed_at,
    )


def process_job_items(job: BatchJob) -> None:
    """Run local normalization + lookup for all PENDING items in *job*.

    Iterates items in row_order order using a DB-side cursor (chunk_size=500)
    so large jobs never materialise all rows in RAM (FR-008).
    Updates each item in the DB and then updates the job counters + status
    via the canonical recompute_job_counters().
    """
    qs = (
        BatchItem.objects.filter(job=job, status=BatchItemStatus.PENDING_NORMALIZATION)
        .order_by("row_order")
        .iterator(chunk_size=500)
    )

    for item in qs:
        status = _process_item(item, remote_enabled=job.remote_enabled)
        item.status = status
        item.save(
            update_fields=[
                "status",
                "detected_type",
                "inchikey",
                "compound",
                "message",
                "original_input",
                "classification_snapshot",
            ]
        )

    recompute_job_counters(job.pk)
    # Refresh in-memory
    job.refresh_from_db()


def create_job(
    rows: Iterable[dict],
    *,
    remote_enabled: bool = True,
) -> tuple[BatchJob, str]:
    """Create a BatchJob, ingest rows, and run local processing.

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
        )
        ingest_rows(job, rows)

    # Local processing runs outside the creation transaction so that a partial
    # error in one item does not roll back the job row itself.
    process_job_items(job)

    return job, plaintext_secret
