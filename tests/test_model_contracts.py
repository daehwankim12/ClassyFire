"""Contract tests for the core data model (Task 3).

These tests pin down the language-agnostic schema guarantees that a future
Go/Rust port must honour: exact column types, source-priority values, row
ordering, the active-InChIKey partial-unique constraint, and the CHAR(27)
check constraint.
"""
from __future__ import annotations

import uuid

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.utils import DataError

from apps.batches.models import BatchItem, BatchJob
from apps.compounds.models import CompoundStructure
from apps.core.enums import (
    SOURCE_PRIORITY,
    BatchItemStatus,
    DetectedInputType,
    SourceType,
    TaskState,
)
from apps.remote.models import RemoteQuery, RemoteQueryItem, RemoteTask

VALID_INCHIKEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"  # aspirin, exactly 27 chars
VALID_INCHIKEY_2 = "RYYVLZVUVIJVGH-UHFFFAOYSA-N"  # caffeine, exactly 27 chars


# ---------------------------------------------------------------------------
# Source priority map
# ---------------------------------------------------------------------------


def test_source_priority_exact_values():
    assert SOURCE_PRIORITY[SourceType.WISHART_LIVE_QUERY] == 100
    assert SOURCE_PRIORITY[SourceType.WISHART_LIVE_ENTITY] == 100
    assert SOURCE_PRIORITY[SourceType.ARCHIVED_WISHART_JSON] == 90
    assert SOURCE_PRIORITY[SourceType.LAB_OFFICIAL_IMPORT] == 85


def test_source_priority_covers_every_source_type():
    assert set(SOURCE_PRIORITY) == set(SourceType)


# ---------------------------------------------------------------------------
# BatchItem row ordering / fields
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_batch_item_preserves_row_order_and_allows_duplicates():
    job = BatchJob.objects.create(secret_hash="x")
    # Insert out of order, with a duplicate original_input.
    BatchItem.objects.create(
        job=job, row_order=2, original_input="CCO", detected_type=DetectedInputType.SMILES,
        status=BatchItemStatus.PENDING_NORMALIZATION,
    )
    BatchItem.objects.create(
        job=job, row_order=0, original_input="CCO", detected_type=DetectedInputType.SMILES,
        status=BatchItemStatus.PENDING_NORMALIZATION, message="dup ok",
    )
    BatchItem.objects.create(
        job=job, row_order=1, original_input="c1ccccc1", detected_type=DetectedInputType.SMILES,
        status=BatchItemStatus.PENDING_NORMALIZATION,
    )

    ordered = list(
        BatchItem.objects.filter(job=job).order_by("row_order").values_list("row_order", flat=True)
    )
    assert ordered == [0, 1, 2]

    first = BatchItem.objects.get(job=job, row_order=0)
    assert first.original_input == "CCO"
    assert first.detected_type == DetectedInputType.SMILES
    assert first.status == BatchItemStatus.PENDING_NORMALIZATION
    assert first.message == "dup ok"

    # Duplicate original_input is allowed.
    assert BatchItem.objects.filter(job=job, original_input="CCO").count() == 2


# ---------------------------------------------------------------------------
# RemoteTask active-InChIKey uniqueness
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_remote_task_active_inchikey_is_unique():
    RemoteTask.objects.create(inchikey=VALID_INCHIKEY, state=TaskState.READY, priority=100)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            RemoteTask.objects.create(
                inchikey=VALID_INCHIKEY, state=TaskState.RUNNING, priority=100
            )


@pytest.mark.django_db
def test_remote_task_done_does_not_conflict():
    # A DONE task does not hold the dedup slot.
    RemoteTask.objects.create(inchikey=VALID_INCHIKEY, state=TaskState.DONE, priority=100)
    # A second DONE task for the same inchikey is fine, and a new ACTIVE one is too.
    RemoteTask.objects.create(inchikey=VALID_INCHIKEY, state=TaskState.DONE, priority=100)
    RemoteTask.objects.create(inchikey=VALID_INCHIKEY, state=TaskState.READY, priority=100)
    assert RemoteTask.objects.filter(inchikey=VALID_INCHIKEY).count() == 3


# ---------------------------------------------------------------------------
# CHAR(27) check constraint
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_inchikey_check_constraint_rejects_wrong_length():
    node_kwargs = {"state": TaskState.READY, "priority": 100}
    for bad in ("BSYNRYMUTXBXSQ-UHFFFAOYSA-",  # 26 chars
                "BSYNRYMUTXBXSQ-UHFFFAOYSA-NN"):  # 28 chars
        with pytest.raises((IntegrityError, DataError)):
            with transaction.atomic():
                RemoteTask.objects.create(inchikey=bad, **node_kwargs)


# ---------------------------------------------------------------------------
# BatchJob UUID PK
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_batch_job_has_uuid_pk():
    job = BatchJob.objects.create(secret_hash="hash")
    assert isinstance(job.id, uuid.UUID)


# ---------------------------------------------------------------------------
# Active-state membership is documented in one place
# ---------------------------------------------------------------------------


def test_task_state_active_states_exclude_done():
    assert TaskState.DONE not in TaskState.active_states()
    assert TaskState.READY in TaskState.active_states()


def test_remote_task_partial_unique_constraint_present():
    """The dedup guarantee is a real partial unique index, not Python-only."""
    constraints = {c.name for c in RemoteTask._meta.constraints}
    assert any("active" in name for name in constraints), constraints


def test_remote_task_has_queue_columns():
    for col in ("state", "priority", "attempt", "next_run_at", "lock_owner", "locked_at"):
        # Raises FieldDoesNotExist if missing.
        RemoteTask._meta.get_field(col)


def test_connection_is_postgres():
    assert connection.vendor == "postgresql"


@pytest.mark.django_db
def test_batch_item_job_row_order_is_unique():
    job = BatchJob.objects.create(secret_hash="x")
    BatchItem.objects.create(job=job, row_order=0, original_input="CCO")

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            BatchItem.objects.create(job=job, row_order=0, original_input="CCC")


@pytest.mark.django_db
def test_compound_structure_inchikey_is_unique():
    CompoundStructure.objects.create(
        original_input="CCO",
        canonical_smiles="CCO",
        inchikey=VALID_INCHIKEY,
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            CompoundStructure.objects.create(
                original_input="duplicate",
                canonical_smiles="CCO",
                inchikey=VALID_INCHIKEY,
            )


@pytest.mark.django_db
def test_inchikey_check_constraint_rejects_bad_format():
    bad_format = "BSYNRYMUTXBXSQ-UHFFFAOYSA-1"
    with pytest.raises((IntegrityError, DataError)):
        with transaction.atomic():
            RemoteTask.objects.create(inchikey=bad_format, state=TaskState.READY)


@pytest.mark.django_db
def test_remote_query_provider_remote_id_is_unique():
    RemoteQuery.objects.create(
        provider="wishart",
        remote_query_id="query-duplicate",
        payload_hash="hash-1",
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            RemoteQuery.objects.create(
                provider="wishart",
                remote_query_id="query-duplicate",
                payload_hash="hash-2",
            )


@pytest.mark.django_db
def test_remote_query_item_query_inchikey_unique():
    """A second item with the same (query, inchikey) is rejected, even with a
    different task (e.g. task=None), so a stale worker cannot re-link a member."""
    query = RemoteQuery.objects.create(
        provider="wishart",
        remote_query_id="query-ik-unique",
        payload_hash="hash",
    )
    task = RemoteTask.objects.create(inchikey=VALID_INCHIKEY, state=TaskState.READY)
    RemoteQueryItem.objects.create(query=query, task=task, inchikey=VALID_INCHIKEY)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            RemoteQueryItem.objects.create(
                query=query, task=None, inchikey=VALID_INCHIKEY
            )


@pytest.mark.django_db
def test_real_postgres_constraints_reject_duplicate_query_item():
    query = RemoteQuery.objects.create(
        provider="wishart",
        remote_query_id="query-items",
        payload_hash="hash",
    )
    task = RemoteTask.objects.create(inchikey=VALID_INCHIKEY, state=TaskState.READY)
    RemoteQueryItem.objects.create(query=query, task=task, inchikey=VALID_INCHIKEY)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            RemoteQueryItem.objects.create(query=query, task=task, inchikey=VALID_INCHIKEY)
