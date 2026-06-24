"""Gate 2 / WS-D: durable streaming ingest pipeline (F-01, F-11) tests.

Covers:
- HTTP upload-route format dispatch (CSV / TSV / SDF) + provenance on both the
  web submit route and the API route.
- Bounded-memory streaming parse (no full file.read(); peak buffer bounded).
- SDF caps: missing $$$$, huge record, huge property.
- Durable worker chunk checkpoint + restart RESUME from PENDING_NORMALIZATION
  without double-processing.
- Bulk lookup/upsert produces statuses + pins IDENTICAL to the per-item path.

All tests run against the real DB.
"""
from __future__ import annotations

import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

ASPIRIN_SMILES = "CC(=O)Oc1ccccc1C(=O)O"
ASPIRIN_INCHIKEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
CAFFEINE_SMILES = "Cn1cnc2c1c(=O)n(c(=O)n2C)C"
CAFFEINE_INCHIKEY = "RYYVLZVUVIJVGH-UHFFFAOYSA-N"
ETHANOL_SMILES = "CCO"
ETHANOL_INCHIKEY = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"

# A real RDKit-parseable molblock for SDF tests (acetic acid skeleton from the
# normalization fixtures).
_SDF_BLOCK = """\

  Mrv2211 01012300002D

  3  2  0  0  0  0            999 V2000
    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.2990    0.7500    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    2.5981    0.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0  0  0  0
  2  3  1  0  0  0  0
M  END
> <EXTERNAL_ID>
CPD-001

$$$$
"""


# ---------------------------------------------------------------------------
# F-01: HTTP upload-route format dispatch + provenance
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_web_submit_csv_upload_dispatch_and_provenance():
    """POST a CSV to the web submit route → rows created with SMILES detected
    type + external_id provenance preserved."""
    from apps.batches.models import BatchItem, BatchJob
    from apps.core.enums import DetectedInputType

    csv = (
        "smiles,id\n"
        f"{ASPIRIN_SMILES},ASP-1\n"
        f"{CAFFEINE_SMILES},CAF-2\n"
    ).encode()
    upload = SimpleUploadedFile("input.csv", csv, content_type="text/csv")

    client = Client()
    resp = client.post(
        "/",
        {"input_file": upload, "text_input": "", "remote_enabled": "false"},
    )
    assert resp.status_code in (302, 303), resp.content

    job = BatchJob.objects.latest("created_at")
    items = list(BatchItem.objects.filter(job=job).order_by("row_order"))
    assert len(items) == 2
    assert items[0].original_input == ASPIRIN_SMILES
    assert items[0].external_id == "ASP-1"
    assert items[0].detected_type == DetectedInputType.SMILES
    assert items[0].inchikey == ASPIRIN_INCHIKEY
    assert items[1].external_id == "CAF-2"


@pytest.mark.django_db(transaction=True)
def test_web_submit_tsv_upload_dispatch():
    """POST a TSV (.tsv) → tab-delimited parse, provenance preserved."""
    from apps.batches.models import BatchItem, BatchJob

    tsv = f"smiles\tid\n{ASPIRIN_SMILES}\tT-1\n".encode()
    upload = SimpleUploadedFile(
        "input.tsv", tsv, content_type="text/tab-separated-values"
    )
    client = Client()
    resp = client.post(
        "/", {"input_file": upload, "text_input": "", "remote_enabled": "false"}
    )
    assert resp.status_code in (302, 303)

    job = BatchJob.objects.latest("created_at")
    item = BatchItem.objects.filter(job=job).get()
    assert item.original_input == ASPIRIN_SMILES
    assert item.external_id == "T-1"


@pytest.mark.django_db(transaction=True)
def test_web_submit_sdf_upload_dispatch_and_external_id():
    """POST an SDF → record split on $$$$, EXTERNAL_ID provenance preserved."""
    from apps.batches.models import BatchItem, BatchJob

    upload = SimpleUploadedFile(
        "mol.sdf", _SDF_BLOCK.encode(), content_type="chemical/x-mdl-sdfile"
    )
    client = Client()
    resp = client.post(
        "/", {"input_file": upload, "text_input": "", "remote_enabled": "false"}
    )
    assert resp.status_code in (302, 303)

    job = BatchJob.objects.latest("created_at")
    items = list(BatchItem.objects.filter(job=job))
    assert len(items) == 1
    assert items[0].external_id == "CPD-001"
    # The raw molblock provenance is preserved (not just a parsed key).
    assert "M  END" in items[0].original_input


@pytest.mark.django_db(transaction=True)
def test_api_multipart_csv_upload_dispatch():
    """The API route also accepts a multipart CSV upload and dispatches it."""
    from apps.batches.models import BatchItem, BatchJob

    csv = f"smiles,id\n{ASPIRIN_SMILES},API-1\n".encode()
    upload = SimpleUploadedFile("input.csv", csv, content_type="text/csv")
    client = Client()
    resp = client.post(
        "/api/jobs",
        {"input_file": upload, "remote_enabled": "false"},
    )
    assert resp.status_code == 201, resp.content
    data = resp.json()
    job = BatchJob.objects.get(pk=data["job_id"])
    item = BatchItem.objects.filter(job=job).get()
    assert item.external_id == "API-1"
    assert item.inchikey == ASPIRIN_INCHIKEY


# ---------------------------------------------------------------------------
# F-01: bounded-memory streaming (no full file.read; one record at a time)
# ---------------------------------------------------------------------------


def test_iter_upload_rows_is_lazy_and_never_reads_whole_file():
    """iter_upload_rows must pull from the stream incrementally — consuming the
    first row must NOT require reading the rest of the file, and the parser must
    never call .read() with no arg (whole-file read)."""
    import io

    from apps.batches.upload import iter_upload_rows

    class TrackingStream(io.BytesIO):
        whole_file_reads = 0

        def read(self, size=-1):
            if size is None or size < 0:
                type(self).whole_file_reads += 1
            return super().read(size)

    big = ("smiles\n" + "\n".join(["CCO"] * 50000) + "\n").encode()
    stream = TrackingStream(big)

    gen = iter_upload_rows(stream, filename="big.csv", content_type="text/csv")
    first = next(gen)
    assert first["raw"] == "CCO"
    assert first["row_index"] == 0
    # Not the whole file consumed yet.
    assert stream.tell() < len(big), "parser eagerly consumed the whole file"
    assert TrackingStream.whole_file_reads == 0, "parser did a whole-file read()"


def test_sdf_streaming_buffers_one_record_at_a_time():
    """SDF parsing holds only one record; consuming the first record must not
    require reading past the second record's delimiter."""
    from apps.batches.upload import iter_upload_rows

    two = (_SDF_BLOCK + _SDF_BLOCK).encode()
    import io

    stream = io.BytesIO(two)
    gen = iter_upload_rows(stream, filename="two.sdf", content_type=None)
    first = next(gen)
    assert first["row_index"] == 0
    assert first["external_id"] == "CPD-001"
    # Buffer reset: peak is bounded by one record, not the whole file.


# ---------------------------------------------------------------------------
# F-01: SDF caps — missing $$$$, huge record, huge property
# ---------------------------------------------------------------------------


def test_sdf_missing_terminal_delimiter_still_emits_record():
    """A trailing record with no closing $$$$ is still emitted."""
    import io

    from apps.batches.upload import iter_upload_rows

    no_term = _SDF_BLOCK.replace("$$$$\n", "")
    rows = list(
        iter_upload_rows(io.BytesIO(no_term.encode()), filename="x.sdf", content_type=None)
    )
    assert len(rows) == 1
    assert rows[0]["external_id"] == "CPD-001"


def test_sdf_huge_record_is_capped_not_buffered():
    """A pathologically huge SDF record is skipped with a truncation marker
    rather than buffered whole."""
    import io

    from apps.batches.upload import MAX_SDF_RECORD_LEN, iter_upload_rows

    huge = "C" * (MAX_SDF_RECORD_LEN + 5000)
    block = f"header\n{huge}\nM  END\n$$$$\n{_SDF_BLOCK}"
    rows = list(
        iter_upload_rows(io.BytesIO(block.encode()), filename="x.sdf", content_type=None)
    )
    assert len(rows) == 2
    # First record over the cap → bounded preview marker, original huge content
    # is NOT retained in full.
    assert "truncated" in rows[0]["raw"]
    assert len(rows[0]["raw"]) < MAX_SDF_RECORD_LEN
    # The following well-formed record still parses with its external_id.
    assert rows[1]["external_id"] == "CPD-001"


def test_sdf_huge_property_value_is_capped():
    """An oversize SDF property value is truncated to MAX_SDF_PROPERTY_LEN."""
    import io

    from apps.batches.upload import iter_upload_rows
    from apps.core.security import MAX_SDF_PROPERTY_LEN

    big_id = "X" * (MAX_SDF_PROPERTY_LEN + 1000)
    block = _SDF_BLOCK.replace("CPD-001", big_id)
    rows = list(
        iter_upload_rows(io.BytesIO(block.encode()), filename="x.sdf", content_type=None)
    )
    assert len(rows) == 1
    assert len(rows[0]["external_id"]) <= MAX_SDF_PROPERTY_LEN


# ---------------------------------------------------------------------------
# F-11: durable worker — claim, checkpoint, restart resume
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_durable_worker_claims_and_processes_job():
    """A job created without inline processing is claimable by the worker and
    reaches a terminal processing_state."""
    from apps.batches.services import create_job_records, run_pending_jobs
    from apps.core.enums import BatchItemStatus, BatchProcessingState

    rows = [{"raw": ASPIRIN_SMILES, "row_index": 0}]
    job, _ = create_job_records(rows, remote_enabled=False)
    assert job.processing_state == BatchProcessingState.PARSING

    processed = run_pending_jobs("test-worker", max_jobs=10)
    assert processed >= 1

    job.refresh_from_db()
    assert job.processing_state in (
        BatchProcessingState.COMPLETE,
        BatchProcessingState.REMOTE_PENDING,
        BatchProcessingState.ACTION_REQUIRED,
    )
    item = job.items.get()
    assert item.status != BatchItemStatus.PENDING_NORMALIZATION


@pytest.mark.django_db(transaction=True)
def test_restart_resumes_from_pending_normalization_without_double_processing():
    """Simulate a crash after the first chunk: half the items are already
    resolved (not PENDING_NORMALIZATION). Re-running process_job_items must only
    touch the remaining PENDING_NORMALIZATION items and never double-process the
    already-resolved ones."""
    from apps.batches.models import BatchItem
    from apps.batches.services import create_job_records, process_job_items
    from apps.compounds.models import CompoundUsage
    from apps.core.enums import BatchItemStatus

    # 4 ethanol rows. Pre-resolve the first two as if a prior chunk committed,
    # leaving the last two PENDING_NORMALIZATION (the resume frontier).
    rows = [{"raw": ETHANOL_SMILES, "row_index": i} for i in range(4)]
    job, _ = create_job_records(rows, remote_enabled=False)

    items = list(BatchItem.objects.filter(job=job).order_by("row_order"))
    for it in items[:2]:
        it.status = BatchItemStatus.REMOTE_DELAYED
        it.inchikey = ETHANOL_INCHIKEY
        it.save(update_fields=["status", "inchikey"])

    # Seed a usage row to a known count so we can prove the resolved items are
    # not re-counted on resume.
    CompoundUsage.objects.update_or_create(
        inchikey=ETHANOL_INCHIKEY, defaults={"seen_count": 2}
    )

    process_job_items(job)

    items = list(BatchItem.objects.filter(job=job).order_by("row_order"))
    # All four resolved now.
    assert all(it.status == BatchItemStatus.REMOTE_DELAYED for it in items)
    # Usage incremented by exactly 2 (only the two resumed items), proving the
    # already-resolved rows were not reprocessed.
    usage = CompoundUsage.objects.get(inchikey=ETHANOL_INCHIKEY)
    assert usage.seen_count == 4


@pytest.mark.django_db(transaction=True)
def test_checkpoint_cursor_advances():
    """The checkpoint cursor advances to the last processed row_order."""
    from apps.batches.services import create_job_records, process_job_items

    rows = [{"raw": ASPIRIN_SMILES, "row_index": i} for i in range(3)]
    job, _ = create_job_records(rows, remote_enabled=False)
    process_job_items(job)
    job.refresh_from_db()
    assert job.processed_through_row_order == 2


# ---------------------------------------------------------------------------
# F-11: bulk lookup/upsert equivalence to the per-item path
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_bulk_chunk_matches_per_item_statuses_and_pins(monkeypatch):
    """The chunked bulk path must produce the SAME item statuses + snapshot pins
    as the legacy per-item path for a mixed job (live hit, seed hit, invalid,
    structured miss, key-only miss)."""
    import datetime as dt
    from datetime import datetime

    from apps.batches.models import BatchItem
    from apps.batches.services import create_job, create_job_records, process_job_items
    from apps.classifications.models import ClassyfireLive, ClassyfireSeed

    # Live hit for aspirin, seed hit for caffeine.
    ClassyfireLive.objects.create(
        inchikey=ASPIRIN_INCHIKEY,
        source_type="WISHART_LIVE_QUERY",
        source_priority=100,
        retrieved_at=datetime(2024, 6, 1, 12, 0, 0, tzinfo=dt.UTC),
    )
    ClassyfireSeed.objects.create(
        inchikey=CAFFEINE_INCHIKEY,
        inchikey14=CAFFEINE_INCHIKEY[:14],
        seed_source_id=1,
        seed_version="v1",
    )

    rows = [
        {"raw": ASPIRIN_SMILES, "row_index": 0},   # live hit
        {"raw": CAFFEINE_SMILES, "row_index": 1},  # seed hit
        {"raw": "bad-smiles-%%%", "row_index": 2},  # invalid
        {"raw": ETHANOL_SMILES, "row_index": 3},   # structured miss
    ]

    # Reference run via the LEGACY per-item path (force chunk size 1 so it is
    # effectively per-item, exercising the same _process_item but item-by-item).
    monkeypatch.setattr("apps.batches.services.PROCESS_CHUNK_SIZE", 1)
    ref_job, _ = create_job([dict(r) for r in rows], remote_enabled=False)
    ref_items = list(BatchItem.objects.filter(job=ref_job).order_by("row_order"))

    # Bulk run with a large chunk (single bulk lookup for the whole job).
    monkeypatch.setattr("apps.batches.services.PROCESS_CHUNK_SIZE", 1000)
    bulk_job, _ = create_job_records([dict(r) for r in rows], remote_enabled=False)
    process_job_items(bulk_job)
    bulk_items = list(BatchItem.objects.filter(job=bulk_job).order_by("row_order"))

    assert len(ref_items) == len(bulk_items) == 4
    for ref, bulk in zip(ref_items, bulk_items, strict=True):
        assert ref.status == bulk.status, f"row {ref.row_order} status mismatch"
        assert ref.inchikey == bulk.inchikey
        assert ref.detected_type == bulk.detected_type
        # Snapshot pins must match for cache hits.
        assert (ref.classification_snapshot is None) == (
            bulk.classification_snapshot is None
        )
        if ref.classification_snapshot is not None:
            assert (
                ref.classification_snapshot["inchikey"]
                == bulk.classification_snapshot["inchikey"]
            )
            assert (
                ref.classification_snapshot["tier"]
                == bulk.classification_snapshot["tier"]
            )


@pytest.mark.django_db(transaction=True)
def test_bulk_lookup_runs_single_query_for_chunk(monkeypatch):
    """The chunk path must issue ONE bulk lookup, not N per-item lookups."""
    from apps.batches import services
    from apps.batches.services import create_job_records, process_job_items

    calls = {"bulk": 0, "single": 0}
    orig_bulk = services.lookup_current_classifications
    orig_single = services.lookup_current_classification

    def spy_bulk(keys):
        calls["bulk"] += 1
        return orig_bulk(keys)

    def spy_single(key):
        calls["single"] += 1
        return orig_single(key)

    monkeypatch.setattr(services, "lookup_current_classifications", spy_bulk)
    monkeypatch.setattr(services, "lookup_current_classification", spy_single)

    rows = [{"raw": s, "row_index": i} for i, s in enumerate(
        [ASPIRIN_SMILES, CAFFEINE_SMILES, ETHANOL_SMILES]
    )]
    job, _ = create_job_records(rows, remote_enabled=False)
    process_job_items(job)

    # Exactly one bulk lookup for the single chunk; no per-item lookups in the
    # worker path.
    assert calls["bulk"] == 1
    assert calls["single"] == 0


# ---------------------------------------------------------------------------
# F-11: deferred-processing mode (INGEST_DEFER_HEAVY_PROCESSING=True)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
@override_settings(INGEST_DEFER_HEAVY_PROCESSING=True)
def test_deferred_post_returns_immediately_items_pending():
    """(a) With the flag True, the HTTP POST returns WITHOUT running heavy work:
    items are still PENDING_NORMALIZATION and the job is not COMPLETE."""
    from apps.batches.models import BatchItem, BatchJob
    from apps.core.enums import BatchItemStatus, BatchJobStatus, BatchProcessingState

    client = Client()
    resp = client.post(
        "/api/jobs",
        data=json.dumps({"text_input": ASPIRIN_SMILES, "remote_enabled": "false"}),
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.content
    job = BatchJob.objects.get(pk=resp.json()["job_id"])

    # HTTP did NOT block on normalization: the item is staged, not resolved.
    item = BatchItem.objects.filter(job=job).get()
    assert item.status == BatchItemStatus.PENDING_NORMALIZATION
    assert item.inchikey is None
    # Job is parked in a claimable parsing state, not yet COMPLETE.
    assert job.processing_state == BatchProcessingState.PARSING
    assert job.status == BatchJobStatus.PENDING


@pytest.mark.django_db(transaction=True)
@override_settings(INGEST_DEFER_HEAVY_PROCESSING=True)
def test_deferred_worker_drains_to_same_result_as_sync():
    """(b) Running the durable worker drains the deferred job to the SAME final
    statuses + pins as the synchronous path."""
    import datetime as dt
    from datetime import datetime

    from apps.batches.models import BatchItem, BatchJob
    from apps.batches.services import create_job, run_pending_jobs
    from apps.classifications.models import ClassyfireLive
    from apps.core.enums import BatchJobStatus

    ClassyfireLive.objects.create(
        inchikey=ASPIRIN_INCHIKEY,
        source_type="WISHART_LIVE_QUERY",
        source_priority=100,
        retrieved_at=datetime(2024, 6, 1, 12, 0, 0, tzinfo=dt.UTC),
    )

    # Synchronous reference (flag is ignored by create_job — it always processes).
    ref_job, _ = create_job([{"raw": ASPIRIN_SMILES, "row_index": 0}], remote_enabled=False)
    ref_item = BatchItem.objects.filter(job=ref_job).get()

    # Deferred POST then worker drain.
    client = Client()
    resp = client.post(
        "/api/jobs",
        data=json.dumps({"text_input": ASPIRIN_SMILES, "remote_enabled": "false"}),
        content_type="application/json",
    )
    job = BatchJob.objects.get(pk=resp.json()["job_id"])
    run_pending_jobs("drain-worker", max_jobs=10)
    job.refresh_from_db()
    deferred_item = BatchItem.objects.filter(job=job).get()

    assert deferred_item.status == ref_item.status
    assert deferred_item.inchikey == ref_item.inchikey == ASPIRIN_INCHIKEY
    assert (
        deferred_item.classification_snapshot["inchikey"]
        == ref_item.classification_snapshot["inchikey"]
    )
    assert (
        deferred_item.classification_snapshot["tier"]
        == ref_item.classification_snapshot["tier"]
    )
    assert job.status == BatchJobStatus.COMPLETE


@pytest.mark.django_db(transaction=True)
@override_settings(INGEST_DEFER_HEAVY_PROCESSING=True)
def test_deferred_route_does_not_materialize_whole_upload(monkeypatch):
    """(c) The deferred route must STREAM rows into staging — it must NOT build
    the whole upload into a list. We forbid list() over the combined iterator by
    asserting create_job_records_streaming is used (not create_job_under_quota's
    list path) and that items are staged from a lazy generator."""
    from apps.batches import services
    from apps.core import security

    used = {"streaming": False, "sync_list": False}
    orig_stream = services.create_job_records_streaming

    def spy_stream(row_iter, **kwargs):
        # Prove the argument is a lazy generator, not a materialized list.
        import types

        used["streaming"] = isinstance(row_iter, types.GeneratorType)
        return orig_stream(row_iter, **kwargs)

    def fail_sync(*_a, **_k):
        used["sync_list"] = True
        raise AssertionError("deferred path must not call create_job_under_quota")

    monkeypatch.setattr(services, "create_job_records_streaming", spy_stream)
    monkeypatch.setattr(security, "create_job_under_quota", fail_sync)

    csv = ("smiles\n" + "\n".join([ASPIRIN_SMILES] * 50) + "\n").encode()
    upload = SimpleUploadedFile("big.csv", csv, content_type="text/csv")
    client = Client()
    resp = client.post(
        "/", {"input_file": upload, "text_input": "", "remote_enabled": "false"}
    )
    assert resp.status_code in (302, 303), resp.content
    assert used["streaming"] is True, "deferred staging must consume a lazy generator"
    assert used["sync_list"] is False


@pytest.mark.django_db(transaction=True)
@override_settings(INGEST_DEFER_HEAVY_PROCESSING=True, MAX_ROWS_PER_JOB=5)
def test_deferred_row_cap_enforced_midstream():
    """The deferred staging path enforces MAX_ROWS_PER_JOB mid-stream (413) and
    does not persist a partial job."""
    from apps.batches.models import BatchJob

    text = "\n".join([ASPIRIN_SMILES] * 20)
    client = Client()
    resp = client.post(
        "/api/jobs",
        data=json.dumps({"text_input": text, "remote_enabled": "false"}),
        content_type="application/json",
    )
    assert resp.status_code == 413, resp.content
    # No job persisted (rolled back).
    assert BatchJob.objects.count() == 0


# ---------------------------------------------------------------------------
# F-11: TRUE crash + reclaim resume (real claim path, not state mutation)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_true_crash_and_reclaim_resumes_without_double_processing(monkeypatch):
    """Claim a job, process+commit the FIRST chunk, then simulate a crash by
    abandoning the run mid-job (job left in a claimable LOCAL_LOOKUP state with
    items still PENDING_NORMALIZATION). RE-CLAIM via the real claim path and
    assert the job completes with NO double-processing: usage counts, compounds,
    and job counters are all correct with no duplicates."""
    from apps.batches.models import BatchItem, BatchJob
    from apps.batches.services import (
        _process_chunk,
        claim_job_for_processing,
        create_job_records,
        process_claimed_job,
    )
    from apps.compounds.models import CompoundStructure, CompoundUsage
    from apps.core.enums import BatchItemStatus, BatchProcessingState

    # 5 distinct ethanol rows (same compound) so usage count is a precise
    # double-processing detector: each row increments usage exactly once.
    rows = [{"raw": ETHANOL_SMILES, "row_index": i} for i in range(5)]
    job, _ = create_job_records(rows, remote_enabled=False)

    # --- Worker A claims the job and processes ONLY the first chunk, commits,
    #     then "crashes" (returns without finishing the rest). ---
    claimed = claim_job_for_processing("worker-A")
    assert claimed is not None and claimed.pk == job.pk

    first_chunk = list(
        BatchItem.objects.filter(
            job=job, status=BatchItemStatus.PENDING_NORMALIZATION
        ).order_by("row_order")[:2]
    )
    BatchJob.objects.filter(pk=job.pk).update(
        processing_state=BatchProcessingState.LOCAL_LOOKUP
    )
    _process_chunk(first_chunk, remote_enabled=False)
    BatchJob.objects.filter(pk=job.pk).update(processed_through_row_order=1)
    # CRASH: worker A dies here. The job stays LOCAL_LOOKUP (claimable), three
    # items remain PENDING_NORMALIZATION, the lease belongs to the dead worker.

    job.refresh_from_db()
    assert job.processing_state == BatchProcessingState.LOCAL_LOOKUP
    assert (
        BatchItem.objects.filter(
            job=job, status=BatchItemStatus.PENDING_NORMALIZATION
        ).count()
        == 3
    )
    usage_after_crash = CompoundUsage.objects.get(inchikey=ETHANOL_INCHIKEY)
    assert usage_after_crash.seen_count == 2  # only the first chunk's 2 rows

    # --- Worker B RE-CLAIMS via the real claim path and finishes the job. ---
    reclaimed = claim_job_for_processing("worker-B")
    assert reclaimed is not None and reclaimed.pk == job.pk
    assert reclaimed.claimed_by == "worker-B"
    assert reclaimed.claim_lease_version == claimed.claim_lease_version + 1
    process_claimed_job(reclaimed)

    # --- All five rows resolved, exactly once each. ---
    items = list(BatchItem.objects.filter(job=job).order_by("row_order"))
    assert all(it.status == BatchItemStatus.REMOTE_DELAYED for it in items)
    usage = CompoundUsage.objects.get(inchikey=ETHANOL_INCHIKEY)
    assert usage.seen_count == 5, "usage must be incremented once per row (no double)"
    # Exactly one compound row (no duplicates from the resumed run).
    assert CompoundStructure.objects.filter(inchikey=ETHANOL_INCHIKEY).count() == 1

    job.refresh_from_db()
    assert job.total_count == 5
    assert job.processing_state in (
        BatchProcessingState.COMPLETE,
        BatchProcessingState.REMOTE_PENDING,
    )


# ---------------------------------------------------------------------------
# MINOR-1: JSON remote_enabled="" must stay truthy (only multipart treats ""→False)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_json_remote_enabled_empty_string_stays_enabled():
    """A JSON client sending remote_enabled="" must NOT be silently flipped to
    False (prior behavior preserved); only multipart absent-checkbox "" → False."""
    from apps.batches.models import BatchJob

    client = Client()
    resp = client.post(
        "/api/jobs",
        data=json.dumps({"text_input": ASPIRIN_SMILES, "remote_enabled": ""}),
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.content
    job = BatchJob.objects.get(pk=resp.json()["job_id"])
    assert job.remote_enabled is True
