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
from collections.abc import Iterable

from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.batches.ingest import ingest_rows
from apps.batches.models import BatchItem, BatchJob
from apps.classifications.services import LookupTier, lookup_current_classification
from apps.compounds.models import CompoundStructure, CompoundUsage
from apps.compounds.validators import normalize_row
from apps.core.enums import (
    BatchItemStatus,
    BatchJobStatus,
    DetectedInputType,
    TaskState,
)
from apps.remote.models import RemoteTask


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
) -> CompoundStructure:
    """Get or create a CompoundStructure for *inchikey*.

    If a structure already exists, we do NOT overwrite it (first-write wins).
    """
    try:
        return CompoundStructure.objects.get(inchikey=inchikey)
    except CompoundStructure.DoesNotExist:
        return CompoundStructure.objects.create(
            inchikey=inchikey,
            original_input=original_input,
            original_smiles=canonical_smiles,  # input may already be canonical
            canonical_smiles=canonical_smiles,
            inchi=inchi,
        )


def _update_usage(inchikey: str) -> None:
    """Upsert CompoundUsage for *inchikey*, incrementing seen_count."""
    usage, created = CompoundUsage.objects.get_or_create(
        inchikey=inchikey,
        defaults={"seen_count": 0},
    )
    if not created:
        CompoundUsage.objects.filter(inchikey=inchikey).update(
            seen_count=usage.seen_count + 1,
            last_seen=timezone.now(),
        )
    else:
        CompoundUsage.objects.filter(inchikey=inchikey).update(seen_count=1)


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
            if result.tier == LookupTier.LIVE:
                return BatchItemStatus.FOUND_LIVE_CACHE
            else:
                return BatchItemStatus.FOUND_SEED_CACHE

        # Step 4: Local miss — create/reuse compound
        compound = _get_or_create_compound(
            norm.inchikey,
            norm.original_input,
            norm.canonical_smiles,
            norm.inchi,
        )
        item.compound = compound
        _update_usage(norm.inchikey)

        # Decision: do we have a structure?
        has_structure = norm.detected_type != DetectedInputType.INCHIKEY

        if has_structure or remote_enabled:
            # Create/reuse a RemoteTask
            _get_or_create_remote_task(
                norm.inchikey,
                compound if has_structure else None,
            )
            return BatchItemStatus.PENDING_CLASSIFICATION
        else:
            # InChIKey-only, remote disabled
            return BatchItemStatus.NEED_STRUCTURE

    # inchikey is None after normalization → the input could not be parsed at all
    # (this shouldn't normally happen — normalize_row returns an error — but be safe)
    item.message = norm.error or "Could not derive InChIKey from input"
    return BatchItemStatus.INVALID_INPUT


def process_job_items(job: BatchJob) -> None:
    """Run local normalization + lookup for all PENDING items in *job*.

    Updates each item in the DB and then updates the job counters + status.
    """
    items = list(
        BatchItem.objects.filter(
            job=job, status=BatchItemStatus.PENDING_NORMALIZATION
        ).order_by("row_order")
    )

    # Process each item
    for item in items:
        status = _process_item(item, remote_enabled=job.remote_enabled)
        item.status = status
        item.save(update_fields=["status", "detected_type", "inchikey", "compound", "message"])

    # Recompute counters from the full item set for this job
    all_items = list(BatchItem.objects.filter(job=job))

    total = len(all_items)
    local_hit = sum(1 for i in all_items if i.status == BatchItemStatus.FOUND_LIVE_CACHE)
    seed_hit = sum(1 for i in all_items if i.status == BatchItemStatus.FOUND_SEED_CACHE)
    remote_pending = sum(
        1 for i in all_items if i.status == BatchItemStatus.PENDING_CLASSIFICATION
    )
    need_structure = sum(1 for i in all_items if i.status == BatchItemStatus.NEED_STRUCTURE)
    invalid = sum(1 for i in all_items if i.status == BatchItemStatus.INVALID_INPUT)

    # "completed" = items that are fully resolved locally
    completed = local_hit + seed_hit

    # Status: COMPLETE only if no item is still pending remote or need_structure
    still_pending = remote_pending + need_structure
    if still_pending == 0:
        job_status = BatchJobStatus.COMPLETE
        job.completed_at = timezone.now()
    else:
        job_status = BatchJobStatus.IN_PROGRESS

    BatchJob.objects.filter(pk=job.pk).update(
        total_count=total,
        completed_count=completed,
        local_hit_count=local_hit,
        seed_hit_count=seed_hit,
        remote_pending_count=remote_pending,
        need_structure_count=need_structure,
        invalid_count=invalid,
        status=job_status,
        completed_at=job.completed_at,
    )
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
