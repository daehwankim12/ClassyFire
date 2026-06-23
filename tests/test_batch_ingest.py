"""Tests for Task 7: Batch Job Ingest and Local Processing.

Covers:
- 100-row mixed input (InChIKey/SMILES/InChI) preserves order + duplicates.
- Invalid row isolated → INVALID_INPUT, others still processed (FR-007).
- Duplicate rows preserved (same input twice → two items, same compound).
- Local LIVE hit → FOUND_LIVE_CACHE.
- Local SEED hit → FOUND_SEED_CACHE.
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


def _make_seed(inchikey: str, kingdom_id: int | None = None) -> Any:
    from apps.classifications.models import ClassyfireSeed

    return ClassyfireSeed.objects.create(
        inchikey=inchikey,
        inchikey14=inchikey[:14],
        seed_source_id=1,
        seed_version="v1",
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_post_jobs_creates_job_and_returns_id_and_secret():
    """POST /api/jobs returns a job id (UUID) and a result secret."""
    client = Client()
    response = _post_job(client, text_input=ASPIRIN_SMILES)
    assert response.status_code == 201, response.content
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

    client = Client()
    response = _post_job(client, text_input=text_input)
    assert response.status_code == 201
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
    client = Client()
    response = _post_job(client, text_input=text_input)
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
    client = Client()
    response = _post_job(client, text_input=text_input)
    assert response.status_code == 201
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

    client = Client()
    response = _post_job(client, text_input=ASPIRIN_SMILES)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    items = list(BatchItem.objects.filter(job=job))
    assert len(items) == 1
    assert items[0].status == BatchItemStatus.FOUND_LIVE_CACHE
    assert items[0].inchikey == ASPIRIN_INCHIKEY


@pytest.mark.django_db(transaction=True)
def test_seed_hit_sets_found_seed_cache():
    """When a SEED classification exists (no live), item gets FOUND_SEED_CACHE."""
    from apps.batches.models import BatchItem, BatchJob
    from apps.core.enums import BatchItemStatus

    node = _make_chemont(11, "Organic compounds")
    _make_seed(ASPIRIN_INCHIKEY, kingdom_id=node.numeric_id)

    client = Client()
    response = _post_job(client, text_input=ASPIRIN_SMILES)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    items = list(BatchItem.objects.filter(job=job))
    assert len(items) == 1
    assert items[0].status == BatchItemStatus.FOUND_SEED_CACHE


@pytest.mark.django_db(transaction=True)
def test_inchikey_only_miss_remote_disabled_sets_need_structure():
    """InChIKey-only input, no classification, remote disabled → NEED_STRUCTURE."""
    from apps.batches.models import BatchItem, BatchJob
    from apps.core.enums import BatchItemStatus

    client = Client()
    response = _post_job(client, text_input=GHOST_INCHIKEY, remote_enabled=False)
    assert response.status_code == 201
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

    client = Client()
    response = _post_job(client, text_input=GHOST_INCHIKEY, remote_enabled=True)
    assert response.status_code == 201
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    items = list(BatchItem.objects.filter(job=job))
    assert len(items) == 1
    assert items[0].status == BatchItemStatus.PENDING_CLASSIFICATION

    # A RemoteTask must exist for this inchikey
    assert RemoteTask.objects.filter(inchikey=GHOST_INCHIKEY).exists()


@pytest.mark.django_db(transaction=True)
def test_smiles_miss_remote_enabled_creates_remote_task():
    """SMILES input, no classification, remote enabled → RemoteTask + PENDING_CLASSIFICATION."""
    from apps.batches.models import BatchItem, BatchJob
    from apps.core.enums import BatchItemStatus
    from apps.remote.models import RemoteTask

    client = Client()
    response = _post_job(client, text_input=GHOST_SMILES, remote_enabled=True)
    assert response.status_code == 201
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    items = list(BatchItem.objects.filter(job=job))
    assert len(items) == 1
    assert items[0].status == BatchItemStatus.PENDING_CLASSIFICATION

    # RemoteTask must exist
    task = RemoteTask.objects.filter(inchikey=items[0].inchikey).first()
    assert task is not None


@pytest.mark.django_db(transaction=True)
def test_global_dedupe_one_active_remote_task():
    """Two jobs with the same inchikey-only miss → exactly ONE active RemoteTask, both items PENDING."""
    from apps.batches.models import BatchItem
    from apps.core.enums import BatchItemStatus, TaskState
    from apps.remote.models import RemoteTask

    client = Client()

    # First job
    r1 = _post_job(client, text_input=GHOST_INCHIKEY, remote_enabled=True)
    assert r1.status_code == 201

    # Second job, same inchikey
    r2 = _post_job(client, text_input=GHOST_INCHIKEY, remote_enabled=True)
    assert r2.status_code == 201

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
    client = Client()
    response = _post_job(client, text_input=text_input)
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
    client = Client()
    response = _post_job(client, text_input=text_input, remote_enabled=False)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    assert job.total_count == 3
    assert job.local_hit_count == 1
    # completed = all terminal items: FOUND_LIVE_CACHE + INVALID_INPUT (unified definition)
    assert job.completed_count == 2
    assert job.invalid_count == 1
    assert job.need_structure_count == 1
    assert job.remote_pending_count == 0
    # NEED_STRUCTURE items do NOT block COMPLETE — only remote_pending items do.
    # This job has no in-flight remote work, so status is COMPLETE.
    assert job.status == BatchJobStatus.COMPLETE


@pytest.mark.django_db(transaction=True)
def test_job_counter_remote_pending():
    """When items go PENDING_CLASSIFICATION, remote_pending_count is set and status IN_PROGRESS."""
    from apps.batches.models import BatchJob
    from apps.core.enums import BatchJobStatus

    client = Client()
    response = _post_job(client, text_input=GHOST_INCHIKEY, remote_enabled=True)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    assert job.total_count == 1
    assert job.remote_pending_count == 1
    assert job.status == BatchJobStatus.IN_PROGRESS


@pytest.mark.django_db(transaction=True)
def test_compound_structure_created_for_smiles():
    """Processing a SMILES creates a CompoundStructure with inchikey + canonical_smiles."""
    from apps.batches.models import BatchItem, BatchJob
    from apps.compounds.models import CompoundStructure

    client = Client()
    response = _post_job(client, text_input=ASPIRIN_SMILES)
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

    client = Client()
    # Process same compound twice across two jobs
    _post_job(client, text_input=ASPIRIN_SMILES)
    _post_job(client, text_input=ASPIRIN_SMILES)

    usage = CompoundUsage.objects.filter(inchikey=ASPIRIN_INCHIKEY).first()
    assert usage is not None
    assert usage.seen_count >= 2


@pytest.mark.django_db(transaction=True)
def test_inchikey_input_links_item_inchikey():
    """A bare InChIKey input is stored on the item even without a structure."""
    from apps.batches.models import BatchItem, BatchJob

    client = Client()
    response = _post_job(client, text_input=GHOST_INCHIKEY, remote_enabled=False)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    item = BatchItem.objects.filter(job=job).first()
    assert item is not None
    assert item.inchikey == GHOST_INCHIKEY


@pytest.mark.django_db(transaction=True)
def test_seed_hit_counter():
    """seed_hit_count is incremented for SEED hits."""
    from apps.batches.models import BatchJob

    _make_seed(ASPIRIN_INCHIKEY)

    client = Client()
    response = _post_job(client, text_input=ASPIRIN_SMILES)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    assert job.seed_hit_count == 1
    assert job.local_hit_count == 0  # seed hits are NOT counted in local_hit_count
    assert job.completed_count == 1


@pytest.mark.django_db(transaction=True)
def test_job_complete_when_all_resolved():
    """Job status is COMPLETE when every item is resolved (no pending items)."""
    from apps.batches.models import BatchJob
    from apps.core.enums import BatchJobStatus

    # Both compounds are in live cache
    _make_live(ASPIRIN_INCHIKEY)
    _make_live(CAFFEINE_INCHIKEY)

    text_input = f"{ASPIRIN_SMILES}\n{CAFFEINE_SMILES}"
    client = Client()
    response = _post_job(client, text_input=text_input)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    assert job.status == BatchJobStatus.COMPLETE


@pytest.mark.django_db(transaction=True)
def test_empty_text_input_returns_empty_job():
    """Empty text_input creates a job with 0 items and status COMPLETE."""
    from apps.batches.models import BatchItem, BatchJob
    from apps.core.enums import BatchJobStatus

    client = Client()
    response = _post_job(client, text_input="")
    assert response.status_code == 201
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    assert BatchItem.objects.filter(job=job).count() == 0
    assert job.total_count == 0
    assert job.status == BatchJobStatus.COMPLETE


@pytest.mark.django_db(transaction=True)
def test_inchi_input_resolves_inchikey():
    """InChI input is normalized and the correct InChIKey is stored on the item."""
    from apps.batches.models import BatchItem, BatchJob

    client = Client()
    response = _post_job(client, text_input=ASPIRIN_INCHI)
    data = response.json()

    job = BatchJob.objects.get(pk=data["job_id"])
    item = BatchItem.objects.filter(job=job).first()
    assert item is not None
    assert item.inchikey == ASPIRIN_INCHIKEY


@pytest.mark.django_db(transaction=True)
def test_remote_task_has_compound_fk_for_smiles():
    """When SMILES → RemoteTask, compound FK is set on the task (structure is known)."""
    from apps.remote.models import RemoteTask

    client = Client()
    response = _post_job(client, text_input=GHOST_SMILES, remote_enabled=True)
    assert response.status_code == 201

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
    """BUG FIX: structure resubmit that resolves to a cache hit must update job counters.

    When the submitted structure is in the seed/live cache, the item flips to
    FOUND_SEED_CACHE/FOUND_LIVE_CACHE with NO remote fan-out.  Previously the
    job's need_structure_count stayed too high and completed_count too low,
    leaving the job stuck IN_PROGRESS forever.  This test asserts the fix.
    """
    import json as _json

    from apps.batches.models import BatchJob
    from apps.core.enums import BatchItemStatus, BatchJobStatus

    client = Client()
    # Create a job with the L-Trp InChIKey (no structure) — remote disabled so
    # the item stays at NEED_STRUCTURE.
    r = _post_job(client, text_input=LTRP_INCHIKEY, remote_enabled=False)
    assert r.status_code == 201
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

    # Now add a seed entry for this InChIKey so the structure resubmit resolves as
    # a seed hit.
    _make_seed(LTRP_INCHIKEY)

    # Submit the matching structure via the API.
    response = client.post(
        f"/api/items/{item.pk}/structure",
        data=_json.dumps({"structure": LTRP_SMILES, "secret": secret}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    resp_data = response.json()
    # Item must now be a cache hit.
    assert resp_data["status"] in (
        BatchItemStatus.FOUND_SEED_CACHE,
        BatchItemStatus.FOUND_LIVE_CACHE,
    ), f"Expected cache-hit status, got {resp_data['status']}"

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
    r = _post_job(client, text_input=LTRP_INCHIKEY, remote_enabled=False)
    assert r.status_code == 201
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
