"""Gate 2 / WS-A: append-only immutable ClassificationResult event log (F-05/F-20).

Verifies that every classification write appends exactly one immutable event,
that the row is never updated/deleted, that key mismatches are STORED (not
discarded), that batch exports pin an immutable event and stay byte-stable after
the live cache is superseded, and that the provenance-preserving official import
path records an artifact + checksum and advances the live pointer.
"""
from __future__ import annotations

import csv
import datetime as dt
import json

import pytest
from django.test import Client

from apps.batches.ingest import parse_text_input
from apps.batches.services import create_job
from apps.classifications.models import (
    ClassificationResult,
    ClassyfireLive,
    SourceArtifact,
)
from apps.classifications.result_writer import write_live_result
from apps.core.enums import ResultKind, SourceType
from apps.remote.provider import ChemOntSlot, NormalizedClassification

ASPIRIN_SMILES = "CC(=O)Oc1ccccc1C(=O)O"
ASPIRIN_INCHIKEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
OTHER_INCHIKEY = "WQZGKKKJIJFFOK-GASJEMHNSA-N"


def _norm(inchikey: str, *, kingdom_name: str = "Organic compounds") -> NormalizedClassification:
    return NormalizedClassification(
        inchikey=inchikey,
        kingdom=ChemOntSlot(chemont_id=None, name=kingdom_name),
        superclass=None,
        class_=None,
        subclass=None,
        direct_parent=None,
        classification_version="cf-2.1",
        raw_json={"inchikey": inchikey, "kingdom": kingdom_name},
    )


# ---------------------------------------------------------------------------
# 1. Every write appends an immutable event; the row is never updated/deleted.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_applied_write_appends_immutable_event_and_sets_live_pointer():
    outcome = write_live_result(
        ASPIRIN_INCHIKEY, _norm(ASPIRIN_INCHIKEY),
        source_type=SourceType.WISHART_LIVE_ENTITY,
    )
    assert outcome.applied is True
    assert outcome.result_id is not None

    event = ClassificationResult.objects.get(pk=outcome.result_id)
    assert event.kind == ResultKind.APPLIED
    assert event.requested_inchikey == ASPIRIN_INCHIKEY
    assert event.returned_inchikey == ASPIRIN_INCHIKEY
    assert len(event.raw_checksum) == 64

    live = ClassyfireLive.objects.get(inchikey=ASPIRIN_INCHIKEY)
    assert live.result_id == event.pk


@pytest.mark.django_db(transaction=True)
def test_supersede_appends_new_event_and_old_event_is_unchanged():
    first = write_live_result(
        ASPIRIN_INCHIKEY, _norm(ASPIRIN_INCHIKEY, kingdom_name="K1"),
        source_type=SourceType.WISHART_LIVE_ENTITY,
        retrieved_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    second = write_live_result(
        ASPIRIN_INCHIKEY, _norm(ASPIRIN_INCHIKEY, kingdom_name="K2"),
        source_type=SourceType.WISHART_LIVE_ENTITY,
        retrieved_at=dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
    )
    assert second.applied is True
    assert second.archived_history is True

    # Two distinct immutable events exist; the first is untouched.
    assert ClassificationResult.objects.count() == 2
    first_event = ClassificationResult.objects.get(pk=first.result_id)
    assert first_event.kind == ResultKind.APPLIED
    assert first_event.labels["kingdom"]["name"] == "K1"

    second_event = ClassificationResult.objects.get(pk=second.result_id)
    assert second_event.kind == ResultKind.SUPERSEDED

    # Live now points at the newest event.
    live = ClassyfireLive.objects.get(inchikey=ASPIRIN_INCHIKEY)
    assert live.result_id == second_event.pk


@pytest.mark.django_db(transaction=True)
def test_lower_priority_write_is_stored_as_rejected_event_without_touching_live():
    applied = write_live_result(
        ASPIRIN_INCHIKEY, _norm(ASPIRIN_INCHIKEY, kingdom_name="High"),
        source_type=SourceType.WISHART_LIVE_QUERY,  # priority 100
    )
    rejected = write_live_result(
        ASPIRIN_INCHIKEY, _norm(ASPIRIN_INCHIKEY, kingdom_name="Low"),
        source_type=SourceType.HISTORICAL_AGGREGATE,  # priority 70
    )
    assert rejected.applied is False
    assert rejected.result_id is not None

    event = ClassificationResult.objects.get(pk=rejected.result_id)
    assert event.kind == ResultKind.REJECTED_LOWER_PRIORITY

    live = ClassyfireLive.objects.get(inchikey=ASPIRIN_INCHIKEY)
    assert live.result_id == applied.result_id  # unchanged


# ---------------------------------------------------------------------------
# 2. A key-mismatch result is STORED (REJECTED_MISMATCH), not discarded.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_key_mismatch_is_stored_as_event_and_live_unchanged():
    # Requested ASPIRIN but source returned a different key.
    norm = _norm(OTHER_INCHIKEY)
    outcome = write_live_result(
        ASPIRIN_INCHIKEY, norm, source_type=SourceType.WISHART_LIVE_QUERY,
    )
    assert outcome.applied is False
    assert outcome.reason == "result key mismatch"
    assert outcome.result_id is not None

    event = ClassificationResult.objects.get(pk=outcome.result_id)
    assert event.kind == ResultKind.REJECTED_MISMATCH
    assert event.requested_inchikey == ASPIRIN_INCHIKEY
    assert event.returned_inchikey == OTHER_INCHIKEY

    # Live was never created for the requested key.
    assert not ClassyfireLive.objects.filter(inchikey=ASPIRIN_INCHIKEY).exists()


# ---------------------------------------------------------------------------
# 3. Export is byte-stable after live is superseded; pins the event, not live.
# ---------------------------------------------------------------------------


def _make_chemont(numeric_id: int, name: str) -> object:
    from apps.chemont.models import ChemOntNode

    return ChemOntNode.objects.create(
        numeric_id=numeric_id,
        chemont_id=f"CHEMONT:{numeric_id:07d}",
        name=name,
        ontology_version="2.1",
    )


def _export_csv_row(client: Client, job_id: str, secret: str) -> dict:
    resp = client.post(
        f"/api/jobs/{job_id}/exports",
        data=json.dumps({"format": "csv", "secret": secret}),
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.content
    token = resp.json()["token"]
    dl = client.get(f"/api/exports/{token}")
    assert dl.status_code == 200
    raw = b"".join(dl.streaming_content) if hasattr(dl, "streaming_content") else dl.content
    lines = [ln for ln in raw.decode("utf-8").splitlines() if not ln.startswith("#")]
    return next(csv.DictReader(lines))


@pytest.mark.django_db(transaction=True)
def test_export_byte_stable_and_pins_event_after_live_supersede():
    _make_chemont(2100, "Organic compounds")  # not used for FK; name-only path
    # Resolve an item via the real writer so resolved_classification is pinned.
    write_live_result(
        ASPIRIN_INCHIKEY, _norm(ASPIRIN_INCHIKEY, kingdom_name="Original kingdom"),
        source_type=SourceType.WISHART_LIVE_ENTITY,
        retrieved_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )

    rows = list(parse_text_input(ASPIRIN_SMILES))
    job, secret = create_job(rows, remote_enabled=False)
    item = job.items.get()
    assert item.resolved_classification_id is not None
    pinned_event_id = item.resolved_classification_id

    client = Client()
    before = _export_csv_row(client, str(job.pk), secret)
    assert before["ClassyFy Status"] == "Completed"
    assert before["Kingdom"] == "Original kingdom"

    # Supersede live with a newer result via the real writer.
    superseded = write_live_result(
        ASPIRIN_INCHIKEY, _norm(ASPIRIN_INCHIKEY, kingdom_name="Replacement kingdom"),
        source_type=SourceType.WISHART_LIVE_ENTITY,
        retrieved_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
    )
    assert superseded.applied is True
    live = ClassyfireLive.objects.get(inchikey=ASPIRIN_INCHIKEY)
    assert live.result_id != pinned_event_id  # live advanced

    after = _export_csv_row(client, str(job.pk), secret)
    # Byte-stable: the item still pins the original event, NOT the new live row.
    assert after == before
    assert after["Kingdom"] == "Original kingdom"

    item.refresh_from_db()
    assert item.resolved_classification_id == pinned_event_id


# ---------------------------------------------------------------------------
# 4. Official import: artifact recorded with checksum, events emitted, live set.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_fanout_resolution_pins_resolved_classification_via_reconcile():
    """The remote fan-out pins the snapshot JSON (with result_id); the batches-side
    reconciliation in recompute_job_counters then pins the FK without re-querying."""
    from apps.batches.models import BatchItem
    from apps.core.enums import BatchItemStatus
    from apps.remote import fanout

    outcome = write_live_result(
        ASPIRIN_INCHIKEY, _norm(ASPIRIN_INCHIKEY),
        source_type=SourceType.WISHART_LIVE_ENTITY,
    )

    # Create a job whose single item is still waiting on the remote result.
    rows = list(parse_text_input(ASPIRIN_SMILES))
    job, _secret = create_job(rows, remote_enabled=True)
    item = job.items.get()
    # Force the waiting state regardless of how local processing resolved it.
    BatchItem.objects.filter(pk=item.pk).update(
        status=BatchItemStatus.PENDING_CLASSIFICATION,
        inchikey=ASPIRIN_INCHIKEY,
        classification_snapshot=None,
        resolved_classification=None,
    )

    fanout.fan_out_result(ASPIRIN_INCHIKEY, BatchItemStatus.FOUND_WISHART_ENTITY)

    item.refresh_from_db()
    assert item.status == BatchItemStatus.FOUND_WISHART_ENTITY
    assert item.resolved_classification_id == outcome.result_id


@pytest.mark.django_db(transaction=True)
def test_official_import_records_artifact_and_advances_live():
    from apps.classifications.official_import import import_official_artifact

    payload = [
        {
            "inchikey": ASPIRIN_INCHIKEY,
            "kingdom": {"chemont_id": None, "name": "Organic compounds"},
            "classification_version": "lab-v1",
            "raw": {"src": "lab", "k": "Organic compounds"},
        }
    ]
    raw = json.dumps(payload).encode("utf-8")

    result = import_official_artifact(raw, name="official_v1.json")

    artifact = SourceArtifact.objects.get(pk=result.artifact_id)
    import hashlib

    assert artifact.checksum == hashlib.sha256(raw).hexdigest()
    assert artifact.byte_size == len(raw)
    assert artifact.source_type == SourceType.LAB_OFFICIAL_IMPORT

    assert result.record_count == 1
    assert result.outcomes[0].applied is True

    event = ClassificationResult.objects.get(pk=result.outcomes[0].result_id)
    assert event.source_type == SourceType.LAB_OFFICIAL_IMPORT
    assert event.source_artifact_id == artifact.pk
    assert event.kind == ResultKind.APPLIED

    live = ClassyfireLive.objects.get(inchikey=ASPIRIN_INCHIKEY)
    assert live.result_id == event.pk
    assert live.source_type == SourceType.LAB_OFFICIAL_IMPORT


@pytest.mark.django_db(transaction=True)
def test_official_import_rejects_malformed_artifact_before_persisting():
    from apps.classifications.official_import import (
        OfficialImportError,
        import_official_artifact,
    )

    with pytest.raises(OfficialImportError):
        import_official_artifact(b"{not json", name="bad.json")

    assert SourceArtifact.objects.count() == 0
    assert ClassificationResult.objects.count() == 0
