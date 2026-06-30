"""Tests for Task 7: Batch Job Ingest and Local Processing.

Covers:
- 100-row mixed input (InChIKey/SMILES/InChI) preserves order + duplicates.
- Invalid row isolated → INVALID_INPUT, others still processed (FR-007).
- Duplicate rows preserved (same input twice → two items, same compound).
- Local LIVE hit → FOUND_LIVE_CACHE.
- InChIKey-only miss with remote DISABLED → NEED_STRUCTURE.
- InChIKey-only miss with remote ENABLED → RemoteTask (entity-lookup), PENDING_CLASSIFICATION.
- Structure (SMILES) miss with remote ENABLED → RemoteTask created, PENDING_CLASSIFICATION.
- Global dedupe: same inchikey across two jobs → exactly ONE active RemoteTask, both items PENDING.
- Job counter updates correct.
All tests run against the real DB — no mocks.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from datetime import datetime
from typing import Any

import pytest
from django.db import IntegrityError
from django.test import Client

# ---------------------------------------------------------------------------
# Test fixtures — well-known InChIKeys for test compounds
# ---------------------------------------------------------------------------

# Aspirin: CC(=O)Oc1ccccc1C(=O)O
ASPIRIN_SMILES = "CC(=O)Oc1ccccc1C(=O)O"
ASPIRIN_INCHIKEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
ASPIRIN_INCHI = "InChI=1S/C9H8O4/c1-6(10)13-8-5-3-2-4-7(8)9(11)12/h2-5H,1H3,(H,11,12)"

# Caffeine: Cn1cnc2c1c(=O)n(c(=O)n2C)C
CAFFEINE_SMILES = "Cn1cnc2c1c(=O)n(c(=O)n2C)C"
CAFFEINE_INCHIKEY = "RYYVLZVUVIJVGH-UHFFFAOYSA-N"

# Ethanol: CCO
ETHANOL_SMILES = "CCO"
ETHANOL_INCHIKEY = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"

# A bare InChIKey that won't be in seed/live (use for miss scenarios)
GHOST_INCHIKEY = "RDHQFKQIGNGIED-MRVPVSSYSA-N"
GHOST_SMILES = "N[C@@H](Cc1c[nH]c2ccccc12)C(=O)O"  # L-Trp canonical form
GHOST_INCHIKEY2 = "QNAYBMKLOCPYGJ-REOHCLBHSA-N"
NONSTANDARD_GHOST_INCHIKEY = "SDIVYZXRQHWCKF-UHFFFAOYNA-N"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _make_chemont(numeric_id: int, name: str) -> Any:
    from apps.chemont.models import ChemOntNode

    node, _ = ChemOntNode.objects.get_or_create(
        numeric_id=numeric_id,
        defaults={
            "chemont_id": f"CHEMONT:{numeric_id:07d}",
            "name": name,
            "ontology_version": "2.1",
        },
    )
    return node


def _make_live(inchikey: str, kingdom_id: int | None = None) -> Any:
    from apps.classifications.models import ClassyfireLive

    return ClassyfireLive.objects.create(
        inchikey=inchikey,
        source_type="WISHART_LIVE_QUERY",
        source_priority=100,
        retrieved_at=datetime(2024, 6, 1, 12, 0, 0, tzinfo=dt.UTC),
        kingdom_id=kingdom_id,
    )


def _post_job(
    client: Client,
    *,
    text_input: str | None = None,
    remote_enabled: bool = True,
    extra_data: dict | None = None,
) -> Any:
    """POST /api/jobs and return the parsed JSON response."""
    data: dict = {"remote_enabled": "true" if remote_enabled else "false"}
    if text_input is not None:
        data["text_input"] = text_input
    if extra_data:
        data.update(extra_data)
    response = client.post(
        "/api/jobs",
        data=json.dumps(data),
        content_type="application/json",
    )
    return response


def _create_and_process_job(
    text_input: str,
    remote_enabled: bool = True,
) -> Any:
    """Create a job and process it synchronously via the service layer.

    Replaces ``_post_job`` for tests that verify processing results (item
    statuses, inchikeys, counters) rather than route-level behaviour.  The
    returned object mimics the JSON response shape so callers only need
    ``data["job_id"]`` and ``data["result_secret"]``.
    """
    from apps.batches.ingest import parse_text_input
    from apps.batches.services import create_job_records, process_job_items

    rows = list(parse_text_input(text_input))
    job, plaintext_secret = create_job_records(rows, remote_enabled=remote_enabled)
    process_job_items(job)

    class _FakeResponse:
        status_code = 202

        def json(self_inner):  # noqa: N805
            return {"job_id": str(job.pk), "result_secret": plaintext_secret}

    return _FakeResponse()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_post_jobs_creates_job_and_returns_id_and_secret():
    """POST /api/jobs returns 202 with a job id (UUID) and a result secret."""
    client = Client()
    response = _post_job(client, text_input=ASPIRIN_SMILES)
    assert response.status_code == 202, response.content
    data = response.json()
    assert "job_id" in data
    assert "result_secret" in data
    # job_id must be a valid UUID
    uuid.UUID(str(data["job_id"]))
    # result_secret must be a non-trivial string
    assert len(data["result_secret"]) >= 32


@pytest.mark.django_db(transaction=True)
def test_secret_hash_stored_not_plaintext():
    """The DB row stores a hash, never the plaintext secret."""
    from apps.batches.models import BatchJob

    client = Client()
    response = _post_job(client, text_input=ASPIRIN_SMILES)
    data = response.json()
    job = BatchJob.objects.get(pk=data["job_id"])
    # The hash is stored, not the plaintext
    assert job.secret_hash != data["result_secret"]
    # The hash must not be empty
    assert job.secret_hash


@pytest.mark.django_db(transaction=True)
def test_100_row_mixed_input_preserves_order_and_count():
    """100 mixed rows (InChIKey/SMILES/InChI) → 100 items in order."""
    from apps.batches.models import BatchItem, BatchJob

    # Build 100 rows: cycle through three types
    rows = []
    for _i in range(34):
        rows.append(ASPIRIN_SMILES)
    for _i in range(33):
        rows.append(ASPIRIN_INCHI)
    for _i in range(33):
        rows.append(ASPIRIN_INCHIKEY)
    text_input = "\n".join(rows)

    response = _create_and_process_job(text_input)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    items = list(BatchItem.objects.filter(job=job).order_by("row_order"))
    assert len(items) == 100
    # row_order is 0-based and sequential
    for i, item in enumerate(items):
        assert item.row_order == i


@pytest.mark.django_db(transaction=True)
def test_duplicate_rows_preserved():
    """Same input twice → two BatchItems, linked to the same CompoundStructure."""
    from apps.batches.models import BatchItem, BatchJob

    text_input = f"{ASPIRIN_SMILES}\n{ASPIRIN_SMILES}"
    response = _create_and_process_job(text_input)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    items = list(BatchItem.objects.filter(job=job).order_by("row_order"))
    assert len(items) == 2
    assert items[0].row_order == 0
    assert items[1].row_order == 1
    # Both point to the same compound (or both have an inchikey)
    assert items[0].inchikey == items[1].inchikey


@pytest.mark.django_db(transaction=True)
def test_invalid_row_isolated_others_processed():
    """One bad row → INVALID_INPUT, the valid rows around it are processed normally.

    FR-007: one bad row must NOT fail the job.
    """
    from apps.batches.models import BatchItem, BatchJob
    from apps.core.enums import BatchItemStatus

    text_input = f"{ASPIRIN_SMILES}\nbad-smiles-%%%\n{CAFFEINE_SMILES}"
    response = _create_and_process_job(text_input)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    items = list(BatchItem.objects.filter(job=job).order_by("row_order"))
    assert len(items) == 3

    # Row 0 and 2 are valid SMILES — they must NOT be INVALID_INPUT
    assert items[0].status != BatchItemStatus.INVALID_INPUT
    # Row 1 is bad
    assert items[1].status == BatchItemStatus.INVALID_INPUT
    assert items[1].message  # message set
    # Row 2 is valid
    assert items[2].status != BatchItemStatus.INVALID_INPUT


@pytest.mark.django_db(transaction=True)
def test_live_hit_sets_found_live_cache():
    """When a LIVE classification exists, item gets FOUND_LIVE_CACHE."""
    from apps.batches.models import BatchItem, BatchJob
    from apps.core.enums import BatchItemStatus

    node = _make_chemont(10, "Chemical entities")
    _make_live(ASPIRIN_INCHIKEY, kingdom_id=node.numeric_id)

    response = _create_and_process_job(ASPIRIN_SMILES)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    items = list(BatchItem.objects.filter(job=job))
    assert len(items) == 1
    assert items[0].status == BatchItemStatus.FOUND_LIVE_CACHE
    assert items[0].inchikey == ASPIRIN_INCHIKEY


@pytest.mark.django_db(transaction=True)
def test_inchikey_only_miss_remote_disabled_sets_need_structure():
    """InChIKey-only input, no classification, remote disabled → NEED_STRUCTURE."""
    from apps.batches.models import BatchItem, BatchJob
    from apps.core.enums import BatchItemStatus

    response = _create_and_process_job(GHOST_INCHIKEY, remote_enabled=False)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    items = list(BatchItem.objects.filter(job=job))
    assert len(items) == 1
    assert items[0].status == BatchItemStatus.NEED_STRUCTURE


@pytest.mark.django_db(transaction=True)
def test_inchikey_only_miss_remote_enabled_creates_remote_task():
    """InChIKey-only input, no classification, remote enabled → RemoteTask + PENDING_CLASSIFICATION."""
    from apps.batches.models import BatchItem, BatchJob
    from apps.core.enums import BatchItemStatus
    from apps.remote.models import RemoteTask

    response = _create_and_process_job(GHOST_INCHIKEY, remote_enabled=True)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    items = list(BatchItem.objects.filter(job=job))
    assert len(items) == 1
    assert items[0].status == BatchItemStatus.PENDING_CLASSIFICATION

    # A RemoteTask must exist for this inchikey
    assert RemoteTask.objects.filter(inchikey=GHOST_INCHIKEY).exists()


@pytest.mark.django_db(transaction=True)
def test_nonstandard_inchikey_only_remote_enabled_waits_for_structure_resolver():
    """A non-standard bare InChIKey enters the structure-resolution path first."""
    from apps.batches.models import BatchItem, BatchJob
    from apps.batches.services import NON_STANDARD_INCHIKEY_MESSAGE
    from apps.core.enums import BatchItemStatus
    from apps.remote.models import RemoteTask

    RemoteTask.objects.filter(inchikey=NONSTANDARD_GHOST_INCHIKEY).delete()

    response = _create_and_process_job(NONSTANDARD_GHOST_INCHIKEY, remote_enabled=True)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    item = BatchItem.objects.get(job=job)
    assert item.status == BatchItemStatus.NEED_STRUCTURE
    assert item.message == NON_STANDARD_INCHIKEY_MESSAGE
    assert not RemoteTask.objects.filter(inchikey=NONSTANDARD_GHOST_INCHIKEY).exists()


@pytest.mark.django_db(transaction=True)
def test_inchikey_only_reusing_retry_wait_task_is_remote_delayed():
    """A new row joining an already-delayed remote task must not appear pending."""
    from django.utils import timezone

    from apps.batches.models import BatchItem, BatchJob
    from apps.core.enums import BatchItemStatus, TaskState
    from apps.remote.models import RemoteTask

    RemoteTask.objects.filter(inchikey=GHOST_INCHIKEY).delete()
    RemoteTask.objects.create(
        inchikey=GHOST_INCHIKEY,
        state=TaskState.RETRY_WAIT,
        next_run_at=timezone.now() + dt.timedelta(minutes=5),
    )

    response = _create_and_process_job(GHOST_INCHIKEY, remote_enabled=True)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    item = BatchItem.objects.get(job=job)
    assert item.status == BatchItemStatus.REMOTE_DELAYED
    assert item.message == "Wishart is retrying this InChIKey; waiting for the next retry window."
    assert RemoteTask.objects.filter(inchikey=GHOST_INCHIKEY).count() == 1


@pytest.mark.django_db(transaction=True)
def test_smiles_miss_remote_enabled_creates_remote_task():
    """SMILES input, no classification, remote enabled → RemoteTask + PENDING_CLASSIFICATION."""
    from apps.batches.models import BatchItem, BatchJob
    from apps.core.enums import BatchItemStatus
    from apps.remote.models import RemoteTask

    response = _create_and_process_job(GHOST_SMILES, remote_enabled=True)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    items = list(BatchItem.objects.filter(job=job))
    assert len(items) == 1
    assert items[0].status == BatchItemStatus.PENDING_CLASSIFICATION

    # RemoteTask must exist
    task = RemoteTask.objects.filter(inchikey=items[0].inchikey).first()
    assert task is not None


@pytest.mark.django_db(transaction=True)
def test_structured_miss_remote_disabled_does_not_create_remote_task():
    """SMILES input with remote disabled must not enqueue remote work."""
    from apps.batches.services import create_job
    from apps.core.enums import BatchItemStatus
    from apps.remote.models import RemoteTask

    rows = [{"raw": ETHANOL_SMILES, "row_index": 0}]

    job, _secret = create_job(rows, remote_enabled=False)
    item = job.items.get()

    assert item.status == BatchItemStatus.REMOTE_DELAYED
    assert item.inchikey == ETHANOL_INCHIKEY
    assert item.compound is not None
    assert RemoteTask.objects.filter(inchikey=ETHANOL_INCHIKEY).count() == 0


@pytest.mark.django_db(transaction=True)
def test_structured_miss_does_not_create_remote_task_when_remote_disabled():
    """Structured miss + remote disabled is delayed locally, not queued."""
    from apps.batches.services import create_job
    from apps.core.enums import BatchItemStatus
    from apps.remote.models import RemoteTask

    job, _secret = create_job(
        [{"raw": ETHANOL_SMILES, "row_index": 0}],
        remote_enabled=False,
    )
    item = job.items.get()

    assert item.status == BatchItemStatus.REMOTE_DELAYED
    assert item.inchikey == ETHANOL_INCHIKEY
    assert item.compound_id is not None
    assert not RemoteTask.objects.filter(inchikey=ETHANOL_INCHIKEY).exists()


@pytest.mark.django_db(transaction=True)
def test_key_only_remote_task_gets_structure_attached_later():
    """A later structured submission attaches compound_id to the active key task."""
    from apps.batches.services import create_job
    from apps.compounds.models import CompoundStructure
    from apps.core.enums import BatchItemStatus
    from apps.remote.models import RemoteTask

    key_job, _secret = create_job(
        [{"raw": ETHANOL_INCHIKEY, "row_index": 0}],
        remote_enabled=True,
    )
    key_item = key_job.items.get()
    task = RemoteTask.objects.get(inchikey=ETHANOL_INCHIKEY)

    assert key_item.status == BatchItemStatus.PENDING_CLASSIFICATION
    assert key_item.compound_id is None
    assert task.compound_id is None
    assert not CompoundStructure.objects.filter(inchikey=ETHANOL_INCHIKEY).exists()

    struct_job, _secret = create_job(
        [{"raw": ETHANOL_SMILES, "row_index": 0}],
        remote_enabled=True,
    )
    struct_item = struct_job.items.get()
    task.refresh_from_db()

    assert struct_item.status == BatchItemStatus.PENDING_CLASSIFICATION
    assert struct_item.compound_id is not None
    assert task.compound_id == struct_item.compound_id
    assert CompoundStructure.objects.filter(inchikey=ETHANOL_INCHIKEY).count() == 1


@pytest.mark.django_db(transaction=True)
def test_global_dedupe_one_active_remote_task():
    """Two jobs with the same inchikey-only miss → exactly ONE active RemoteTask, both items PENDING."""
    from apps.batches.models import BatchItem
    from apps.core.enums import BatchItemStatus, TaskState
    from apps.remote.models import RemoteTask

    # First job
    r1 = _create_and_process_job(GHOST_INCHIKEY, remote_enabled=True)
    # Second job, same inchikey
    r2 = _create_and_process_job(GHOST_INCHIKEY, remote_enabled=True)

    d1, d2 = r1.json(), r2.json()

    # Both items must be PENDING_CLASSIFICATION
    item1 = BatchItem.objects.filter(job_id=d1["job_id"]).first()
    item2 = BatchItem.objects.filter(job_id=d2["job_id"]).first()
    assert item1 is not None and item1.status == BatchItemStatus.PENDING_CLASSIFICATION
    assert item2 is not None and item2.status == BatchItemStatus.PENDING_CLASSIFICATION

    # Exactly ONE active RemoteTask for this inchikey
    active_tasks = RemoteTask.objects.filter(
        inchikey=GHOST_INCHIKEY,
        state__in=[s.value for s in TaskState.active_states()],
    )
    assert active_tasks.count() == 1


@pytest.mark.django_db(transaction=True)
def test_job_counters_all_local_hits():
    """Job counters: all local live hits → local_hit_count, completed_count, status COMPLETE."""
    from apps.batches.models import BatchJob
    from apps.core.enums import BatchJobStatus

    _make_live(ASPIRIN_INCHIKEY)

    text_input = f"{ASPIRIN_SMILES}\n{ASPIRIN_INCHI}\n{ASPIRIN_INCHIKEY}"
    response = _create_and_process_job(text_input)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    assert job.total_count == 3
    assert job.local_hit_count == 3
    assert job.completed_count == 3
    assert job.invalid_count == 0
    assert job.need_structure_count == 0
    assert job.remote_pending_count == 0
    assert job.status == BatchJobStatus.COMPLETE


@pytest.mark.django_db(transaction=True)
def test_job_counters_mixed():
    """Job counters: mix of live hit, invalid, need_structure, remote_pending."""
    from apps.batches.models import BatchJob
    from apps.core.enums import BatchJobStatus

    _make_live(ASPIRIN_INCHIKEY)

    # live hit + invalid + inchikey miss (remote off) + inchikey miss (remote on)
    # We only do 1 job; to keep it simple use remote_enabled=False
    # line0=aspirin (live), line1=bad, line2=ghost_inchikey (need_structure remote off)
    text_input = f"{ASPIRIN_SMILES}\nbad-smiles-%%%\n{GHOST_INCHIKEY}"
    response = _create_and_process_job(text_input, remote_enabled=False)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    assert job.total_count == 3
    assert job.local_hit_count == 1
    # completed = all terminal items: FOUND_LIVE_CACHE + INVALID_INPUT (unified definition)
    assert job.completed_count == 2
    assert job.invalid_count == 1
    assert job.need_structure_count == 1
    assert job.remote_pending_count == 0
    # NEED_STRUCTURE items do not block processing, but the job is not plain
    # COMPLETE because user action is still required.
    assert job.status == BatchJobStatus.COMPLETE_WITH_ACTION_REQUIRED


@pytest.mark.django_db(transaction=True)
def test_job_counter_remote_pending():
    """When items go PENDING_CLASSIFICATION, remote_pending_count is set and status IN_PROGRESS."""
    from apps.batches.models import BatchJob
    from apps.core.enums import BatchJobStatus

    response = _create_and_process_job(GHOST_INCHIKEY, remote_enabled=True)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    assert job.total_count == 1
    assert job.remote_pending_count == 1
    assert job.status == BatchJobStatus.IN_PROGRESS


@pytest.mark.django_db(transaction=True)
def test_pending_normalization_keeps_job_processing():
    """PENDING_NORMALIZATION is blocking work, so the job is not terminal."""
    from django.utils import timezone

    from apps.batches.models import BatchItem, BatchJob
    from apps.batches.services import recompute_job_counters
    from apps.core.enums import BatchItemStatus, BatchJobStatus, DetectedInputType

    completed_at = timezone.now()
    job = BatchJob.objects.create(
        secret_hash="abc123",
        status=BatchJobStatus.COMPLETE,
        completed_at=completed_at,
    )
    BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=ASPIRIN_SMILES,
        detected_type=DetectedInputType.SMILES,
        status=BatchItemStatus.PENDING_NORMALIZATION,
    )

    recompute_job_counters(job.pk)

    job.refresh_from_db()
    assert job.total_count == 1
    assert job.completed_count == 0
    assert job.status == BatchJobStatus.IN_PROGRESS
    assert job.completed_at is None


@pytest.mark.django_db(transaction=True)
def test_need_structure_only_job_is_action_required_not_plain_complete():
    """A job with only NEED_STRUCTURE rows is terminal but requires user action."""
    from apps.batches.models import BatchItem, BatchJob
    from apps.batches.services import recompute_job_counters
    from apps.core.enums import BatchItemStatus, DetectedInputType

    job = BatchJob.objects.create(secret_hash="abc123")
    BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=GHOST_INCHIKEY,
        detected_type=DetectedInputType.INCHIKEY,
        status=BatchItemStatus.NEED_STRUCTURE,
    )

    recompute_job_counters(job.pk)

    job.refresh_from_db()
    assert job.total_count == 1
    assert job.need_structure_count == 1
    assert job.completed_count == 0
    assert job.status == "COMPLETE_WITH_ACTION_REQUIRED"
    assert job.completed_at is not None


@pytest.mark.django_db(transaction=True)
def test_recompute_counters_locks_job_row(monkeypatch):
    """Counter recompute must lock the BatchJob row before deriving counters."""
    from django.db.models import QuerySet

    from apps.batches.models import BatchItem, BatchJob
    from apps.batches.services import recompute_job_counters
    from apps.core.enums import BatchItemStatus, DetectedInputType

    job = BatchJob.objects.create(secret_hash="abc123")
    BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=ASPIRIN_SMILES,
        detected_type=DetectedInputType.SMILES,
        status=BatchItemStatus.FOUND_LIVE_CACHE,
    )

    original = QuerySet.select_for_update
    calls: list[str] = []

    def spy_select_for_update(self, *args, **kwargs):
        if self.model is BatchJob:
            calls.append(self.model.__name__)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", spy_select_for_update)

    recompute_job_counters(job.pk)

    assert calls == ["BatchJob"]


@pytest.mark.django_db(transaction=True)
def test_action_required_job_is_terminal_for_detail_and_partial(monkeypatch):
    from django.test import RequestFactory

    from apps.batches import views
    from apps.batches.models import BatchItem, BatchJob
    from apps.core.enums import BatchItemStatus, BatchJobStatus, DetectedInputType

    captured: list[dict] = []

    def fake_render(_request, _template, context):
        captured.append(context)
        from django.http import HttpResponse

        return HttpResponse("ok")

    monkeypatch.setattr(views, "_has_job_access", lambda _request, _job, _secret: True)
    monkeypatch.setattr(views, "render", fake_render)

    job = BatchJob.objects.create(
        secret_hash="abc123",
        status=BatchJobStatus.COMPLETE_WITH_ACTION_REQUIRED,
        total_count=1,
        need_structure_count=1,
    )
    BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=GHOST_INCHIKEY,
        detected_type=DetectedInputType.INCHIKEY,
        status=BatchItemStatus.NEED_STRUCTURE,
    )
    request = RequestFactory().get("/")

    views.job_detail_view(request, str(job.pk))
    views.item_table_partial_view(request, str(job.pk))

    assert [context["job_complete"] for context in captured] == [True, True]


@pytest.mark.django_db(transaction=True)
def test_stats_counts_action_required_jobs_as_complete(monkeypatch):
    from django.test import RequestFactory

    from apps.batches import views
    from apps.batches.models import BatchJob
    from apps.core.enums import BatchJobStatus

    captured: dict = {}

    def fake_render(_request, _template, context):
        captured.update(context)
        from django.http import HttpResponse

        return HttpResponse("ok")

    monkeypatch.setattr(views, "render", fake_render)

    BatchJob.objects.create(secret_hash="complete", status=BatchJobStatus.COMPLETE)
    BatchJob.objects.create(
        secret_hash="action-required",
        status=BatchJobStatus.COMPLETE_WITH_ACTION_REQUIRED,
    )

    views.stats_view(RequestFactory().get("/stats/"))

    assert captured["jobs_complete"] == 2


@pytest.mark.django_db(transaction=True)
def test_job_detail_pending_count_includes_normalization(monkeypatch):
    from django.test import RequestFactory

    from apps.batches import views
    from apps.batches.models import BatchItem, BatchJob
    from apps.core.enums import BatchItemStatus, BatchJobStatus, DetectedInputType

    captured: dict = {}

    def fake_render(_request, _template, context):
        captured.update(context)
        from django.http import HttpResponse

        return HttpResponse("ok")

    monkeypatch.setattr(views, "_has_job_access", lambda _request, _job, _secret: True)
    monkeypatch.setattr(views, "render", fake_render)

    job = BatchJob.objects.create(
        secret_hash="abc123",
        status=BatchJobStatus.IN_PROGRESS,
        total_count=2,
        remote_pending_count=1,
    )
    BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=ASPIRIN_SMILES,
        detected_type=DetectedInputType.SMILES,
        status=BatchItemStatus.PENDING_NORMALIZATION,
    )
    BatchItem.objects.create(
        job=job,
        row_order=1,
        original_input=GHOST_INCHIKEY,
        detected_type=DetectedInputType.INCHIKEY,
        status=BatchItemStatus.PENDING_CLASSIFICATION,
    )

    views.job_detail_view(RequestFactory().get("/"), str(job.pk))

    assert captured["blocking_pending_count"] == 2


@pytest.mark.django_db(transaction=True)
def test_compound_structure_created_for_smiles():
    """Processing a SMILES creates a CompoundStructure with inchikey + canonical_smiles."""
    from apps.batches.models import BatchItem, BatchJob
    from apps.compounds.models import CompoundStructure

    response = _create_and_process_job(ASPIRIN_SMILES)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    item = BatchItem.objects.filter(job=job).first()
    assert item is not None
    assert item.inchikey == ASPIRIN_INCHIKEY

    struct = CompoundStructure.objects.filter(inchikey=ASPIRIN_INCHIKEY).first()
    assert struct is not None
    assert struct.canonical_smiles is not None


@pytest.mark.django_db(transaction=True)
def test_compound_usage_updated():
    """CompoundUsage is created/updated for each processed (non-invalid) row."""
    from apps.compounds.models import CompoundUsage

    # Process same compound twice across two jobs
    _create_and_process_job(ASPIRIN_SMILES)
    _create_and_process_job(ASPIRIN_SMILES)

    usage = CompoundUsage.objects.filter(inchikey=ASPIRIN_INCHIKEY).first()
    assert usage is not None
    assert usage.seen_count >= 2


@pytest.mark.django_db(transaction=True)
def test_inchikey_input_links_item_inchikey():
    """A bare InChIKey input is stored on the item even without a structure."""
    from apps.batches.models import BatchItem, BatchJob

    response = _create_and_process_job(GHOST_INCHIKEY, remote_enabled=False)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    item = BatchItem.objects.filter(job=job).first()
    assert item is not None
    assert item.inchikey == GHOST_INCHIKEY


@pytest.mark.django_db(transaction=True)
def test_job_complete_when_all_resolved():
    """Job status is COMPLETE when every item is resolved (no pending items)."""
    from apps.batches.models import BatchJob
    from apps.core.enums import BatchJobStatus

    # Both compounds are in live cache
    _make_live(ASPIRIN_INCHIKEY)
    _make_live(CAFFEINE_INCHIKEY)

    text_input = f"{ASPIRIN_SMILES}\n{CAFFEINE_SMILES}"
    response = _create_and_process_job(text_input)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    assert job.status == BatchJobStatus.COMPLETE


@pytest.mark.django_db(transaction=True)
def test_empty_text_input_returns_empty_job():
    """Empty text_input creates a job with 0 items and status COMPLETE."""
    from apps.batches.models import BatchItem, BatchJob
    from apps.core.enums import BatchJobStatus

    response = _create_and_process_job("")
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    assert BatchItem.objects.filter(job=job).count() == 0
    assert job.total_count == 0
    assert job.status == BatchJobStatus.COMPLETE


@pytest.mark.django_db(transaction=True)
def test_inchi_input_resolves_inchikey():
    """InChI input is normalized and the correct InChIKey is stored on the item."""
    from apps.batches.models import BatchItem, BatchJob

    response = _create_and_process_job(ASPIRIN_INCHI)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    item = BatchItem.objects.filter(job=job).first()
    assert item is not None
    assert item.inchikey == ASPIRIN_INCHIKEY


@pytest.mark.django_db(transaction=True)
def test_remote_task_has_compound_fk_for_smiles():
    """When SMILES → RemoteTask, compound FK is set on the task (structure is known)."""
    from apps.remote.models import RemoteTask

    _create_and_process_job(GHOST_SMILES, remote_enabled=True)

    task = RemoteTask.objects.filter(inchikey__isnull=False).first()
    assert task is not None
    assert task.compound is not None  # structure known, FK set


@pytest.mark.django_db(transaction=True)
def test_get_or_create_compound_already_exists_no_error():
    """_get_or_create_compound returns existing row when called twice (race-safe)."""
    from apps.batches.services import _get_or_create_compound
    from apps.compounds.models import CompoundStructure

    # Pre-create the compound
    CompoundStructure.objects.create(
        inchikey=ASPIRIN_INCHIKEY,
        original_input=ASPIRIN_SMILES,
        original_smiles=ASPIRIN_SMILES,
        canonical_smiles=ASPIRIN_SMILES,
        inchi=ASPIRIN_INCHI,
    )

    # Second call must return the existing row without error
    compound = _get_or_create_compound(
        ASPIRIN_INCHIKEY, ASPIRIN_SMILES, ASPIRIN_SMILES, ASPIRIN_INCHI
    )
    assert compound.inchikey == ASPIRIN_INCHIKEY
    # Only one row exists (no duplicate created)
    assert CompoundStructure.objects.filter(inchikey=ASPIRIN_INCHIKEY).count() == 1


@pytest.mark.django_db(transaction=True)
def test_get_or_create_compound_recovers_from_concurrent_unique_race(monkeypatch):
    """Concurrent structure creates must converge on one unique compound row."""
    from apps.batches import services
    from apps.batches.services import _get_or_create_compound
    from apps.compounds.models import CompoundStructure

    CompoundStructure.objects.create(
        inchikey=ETHANOL_INCHIKEY,
        original_input=ETHANOL_INCHIKEY,
    )

    class FirstLookupMiss:
        def filter(self, **_kwargs):
            return self

        def order_by(self, *_fields):
            return self

        def first(self):
            return None

    original_select_for_update = services.CompoundStructure.objects.select_for_update
    select_for_update_calls = 0

    def fake_select_for_update(*args, **kwargs):
        nonlocal select_for_update_calls
        select_for_update_calls += 1
        if select_for_update_calls == 1:
            return FirstLookupMiss()
        return original_select_for_update(*args, **kwargs)

    def raise_concurrent_unique_insert(*_args, **_kwargs):
        raise IntegrityError("simulated concurrent unique insert")

    monkeypatch.setattr(
        services.CompoundStructure.objects,
        "create",
        raise_concurrent_unique_insert,
    )
    monkeypatch.setattr(
        services.CompoundStructure.objects,
        "select_for_update",
        fake_select_for_update,
    )

    compound = _get_or_create_compound(
        ETHANOL_INCHIKEY,
        ETHANOL_SMILES,
        ETHANOL_SMILES,
        None,
    )

    assert compound is not None
    compound = CompoundStructure.objects.get(inchikey=ETHANOL_INCHIKEY)
    assert compound.canonical_smiles == ETHANOL_SMILES
    assert compound.original_input == ETHANOL_SMILES
    assert CompoundStructure.objects.filter(inchikey=ETHANOL_INCHIKEY).count() == 1


# ---------------------------------------------------------------------------
# Structure resubmit: job counter + status update (bug fix)
# ---------------------------------------------------------------------------

# L-Tryptophan: SMILES → InChIKey are a matched pair verified via RDKit.
# Using a separate InChIKey/SMILES pair for structure-resubmit tests so they
# are independent of GHOST_INCHIKEY (which is a bare key with no matching SMILES
# in this file).
LTRP_SMILES = "N[C@@H](Cc1c[nH]c2ccccc12)C(=O)O"
LTRP_INCHIKEY = "QIVBCDIJIAJPQS-VIFPVBQESA-N"


@pytest.mark.django_db(transaction=True)
def test_api_structure_resubmit_cache_hit_updates_counters():
    """Structure resubmit that resolves to a cache hit must update job counters.

    When the submitted structure is in the live cache, the item flips to
    FOUND_LIVE_CACHE with no remote fan-out, so counters must be recomputed
    immediately in the resubmit path.
    """
    import json as _json

    from apps.batches.models import BatchJob
    from apps.core.enums import BatchItemStatus, BatchJobStatus

    client = Client()
    # Create a job with the L-Trp InChIKey (no structure) — remote disabled so
    # the item stays at NEED_STRUCTURE.  Use the service layer directly so the
    # sync pipeline runs regardless of INGEST_DEFER_HEAVY_PROCESSING.
    r = _create_and_process_job(LTRP_INCHIKEY, remote_enabled=False)
    data = r.json()
    job = BatchJob.objects.get(pk=data["job_id"])
    secret = data["result_secret"]

    # Sanity: item is NEED_STRUCTURE, job counters reflect that.
    item = job.items.first()
    assert item is not None
    assert item.status == BatchItemStatus.NEED_STRUCTURE
    assert item.inchikey == LTRP_INCHIKEY
    job.refresh_from_db()
    assert job.need_structure_count == 1
    assert job.completed_count == 0

    # Now add a live entry for this InChIKey so the structure resubmit resolves as
    # a live cache hit.
    _make_live(LTRP_INCHIKEY)

    # Submit the matching structure via the API.
    response = client.post(
        f"/api/items/{item.pk}/structure",
        data=_json.dumps({"structure": LTRP_SMILES, "secret": secret}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    resp_data = response.json()
    # Item must now be a cache hit.
    assert resp_data["status"] == BatchItemStatus.FOUND_LIVE_CACHE, (
        f"Expected cache-hit status, got {resp_data['status']}"
    )

    # Job counters MUST have been updated immediately (no fan-out required).
    job.refresh_from_db()
    assert job.need_structure_count == 0, (
        "need_structure_count must be decremented after cache-hit structure resubmit"
    )
    assert job.completed_count == 1, (
        "completed_count must be incremented after cache-hit structure resubmit"
    )
    # The only item is now terminal — job must be COMPLETE.
    assert job.status == BatchJobStatus.COMPLETE, (
        "job must transition to COMPLETE when last in-flight item resolves via cache hit"
    )


@pytest.mark.django_db(transaction=True)
def test_api_structure_resubmit_pending_updates_counters():
    """Structure resubmit that is a cache miss → PENDING_CLASSIFICATION; counters updated."""
    import json as _json

    from apps.batches.models import BatchJob
    from apps.core.enums import BatchItemStatus, BatchJobStatus

    client = Client()
    # Create a NEED_STRUCTURE job (remote disabled, L-Trp InChIKey, no seed/live data).
    # Use the service layer directly so the sync pipeline runs regardless of
    # INGEST_DEFER_HEAVY_PROCESSING.
    r = _create_and_process_job(LTRP_INCHIKEY, remote_enabled=False)
    data = r.json()
    job = BatchJob.objects.get(pk=data["job_id"])
    secret = data["result_secret"]

    item = job.items.first()
    assert item is not None
    assert item.status == BatchItemStatus.NEED_STRUCTURE
    assert item.inchikey == LTRP_INCHIKEY
    job.refresh_from_db()
    assert job.need_structure_count == 1

    # Manually enable remote on the job so the resubmit path creates a RemoteTask.
    BatchJob.objects.filter(pk=job.pk).update(remote_enabled=True)
    job.refresh_from_db()

    # Submit the matching structure — no cache entry, so item goes PENDING_CLASSIFICATION.
    response = client.post(
        f"/api/items/{item.pk}/structure",
        data=_json.dumps({"structure": LTRP_SMILES, "secret": secret}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert response.json()["status"] == BatchItemStatus.PENDING_CLASSIFICATION

    # Counters must reflect the transition.
    job.refresh_from_db()
    assert job.need_structure_count == 0, (
        "need_structure_count must drop to 0 after pending-resolution structure resubmit"
    )
    assert job.remote_pending_count == 1, (
        "remote_pending_count must rise to 1 after pending-resolution structure resubmit"
    )
    # Job still has in-flight remote work.
    assert job.status == BatchJobStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# Overlong row handling: preserve bounded original_input + explicit message
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_overlong_row_preserves_bounded_original_input_and_message():
    """An over-limit row must become INVALID_INPUT with:
    - original_input = a bounded preview of the real input (never the sentinel)
    - message = explicit reason mentioning the limit
    Other rows in the same job must be processed normally.
    """
    from apps.batches.services import create_job
    from apps.core.enums import BatchItemStatus
    from apps.core.security import MAX_STRUCTURE_LEN

    long_structure = "N" * (MAX_STRUCTURE_LEN + 500)
    rows = [
        {"raw": ASPIRIN_SMILES, "row_index": 0},
        {"raw": long_structure, "row_index": 1},
        {"raw": CAFFEINE_SMILES, "row_index": 2},
    ]

    job, _secret = create_job(rows, remote_enabled=False)
    items = list(job.items.order_by("row_order"))
    assert len(items) == 3

    # Valid rows must not be INVALID_INPUT.
    assert items[0].status != BatchItemStatus.INVALID_INPUT
    assert items[2].status != BatchItemStatus.INVALID_INPUT

    overlong_item = items[1]
    assert overlong_item.status == BatchItemStatus.INVALID_INPUT

    # Must NOT be the synthetic sentinel.
    assert "__INVALID__ROW_TOO_LONG__" not in overlong_item.original_input, (
        "original_input must not be the synthetic sentinel string"
    )
    # Must start with the real submitted characters (N).
    assert overlong_item.original_input.startswith("N"), (
        "original_input must start with the real submitted content"
    )
    # Must contain the truncation marker.
    assert "truncated" in overlong_item.original_input, (
        "original_input must contain a truncation marker"
    )
    # message must mention the limit.
    assert overlong_item.message, "message must be non-empty for an overlong row"
    assert str(MAX_STRUCTURE_LEN) in overlong_item.message, (
        f"message should reference the max length ({MAX_STRUCTURE_LEN}), "
        f"got: {overlong_item.message}"
    )
