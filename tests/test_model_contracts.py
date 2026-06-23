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
from apps.chemont.models import ChemOntNode
from apps.classifications.models import ClassyfireSeed
from apps.core.enums import (
    SOURCE_PRIORITY,
    BatchItemStatus,
    DetectedInputType,
    SourceType,
    TaskState,
)
from apps.remote.models import RemoteTask

VALID_INCHIKEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"  # aspirin, exactly 27 chars
VALID_INCHIKEY_2 = "RYYVLZVUVIJVGH-UHFFFAOYSA-N"  # caffeine, exactly 27 chars


# ---------------------------------------------------------------------------
# ClassyfireSeed shape
# ---------------------------------------------------------------------------


def _field(model, name):
    return model._meta.get_field(name)


def test_classyfire_seed_inchikey_is_char27():
    field = _field(ClassyfireSeed, "inchikey")
    assert field.get_internal_type() == "CharField"
    assert field.max_length == 27
    assert field.primary_key is True


def test_classyfire_seed_inchikey14_is_char14():
    field = _field(ClassyfireSeed, "inchikey14")
    assert field.get_internal_type() == "CharField"
    assert field.max_length == 14
    assert field.null is False


def test_classyfire_seed_has_five_chemont_slots():
    for slot in (
        "kingdom",
        "superclass",
        "class_node",
        "subclass",
        "direct_parent",
    ):
        field = _field(ClassyfireSeed, slot)
        assert field.is_relation, f"{slot} must be a FK"
        assert field.related_model is ChemOntNode


def test_classyfire_seed_has_source_and_version():
    seed_source = _field(ClassyfireSeed, "seed_source_id")
    assert seed_source.get_internal_type() == "BigIntegerField"
    assert seed_source.null is False

    seed_version = _field(ClassyfireSeed, "seed_version")
    assert seed_version.get_internal_type() in {"CharField", "TextField"}
    assert seed_version.null is False


def test_classyfire_seed_db_table():
    assert ClassyfireSeed._meta.db_table == "classyfire_seed"


# ---------------------------------------------------------------------------
# Source priority map
# ---------------------------------------------------------------------------


def test_source_priority_exact_values():
    assert SOURCE_PRIORITY[SourceType.WISHART_LIVE_QUERY] == 100
    assert SOURCE_PRIORITY[SourceType.WISHART_LIVE_ENTITY] == 100
    assert SOURCE_PRIORITY[SourceType.ARCHIVED_WISHART_JSON] == 90
    assert SOURCE_PRIORITY[SourceType.LAB_OFFICIAL_IMPORT] == 85
    assert SOURCE_PRIORITY[SourceType.HISTORICAL_AGGREGATE] == 70


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


@pytest.mark.django_db
def test_seed_inchikey14_check_constraint():
    """A seed with a non-14-char inchikey14 is rejected by the DB."""
    with pytest.raises((IntegrityError, DataError)):
        with transaction.atomic():
            ClassyfireSeed.objects.create(
                inchikey=VALID_INCHIKEY,
                inchikey14="TOOSHORT",  # not 14 chars
                seed_source_id=1,
                seed_version="v1",
            )


@pytest.mark.django_db
def test_seed_round_trip_with_valid_lengths():
    node = ChemOntNode.objects.create(
        numeric_id=1, chemont_id="CHEMONT:0000000", name="root", ontology_version="2.1"
    )
    seed = ClassyfireSeed.objects.create(
        inchikey=VALID_INCHIKEY,
        inchikey14=VALID_INCHIKEY[:14],
        seed_source_id=42,
        seed_version="v1.0",
        kingdom=node,
        other_labels={"foo": "bar"},
    )
    seed.refresh_from_db()
    assert seed.inchikey14 == VALID_INCHIKEY[:14]
    assert seed.kingdom_id == 1
    assert seed.other_labels == {"foo": "bar"}


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
