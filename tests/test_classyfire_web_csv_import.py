from __future__ import annotations

import hashlib
import json

import pytest
from django.core.management import call_command

from apps.batches.models import BatchItem, BatchJob
from apps.chemont.models import ChemOntNode
from apps.classifications.models import (
    ClassificationResult,
    ClassyfireLive,
    SourceArtifact,
)
from apps.core.enums import (
    BatchItemStatus,
    QueryState,
    RemoteQueryItemApplyState,
    ResultKind,
    SourceType,
    TaskState,
)
from apps.remote import queue
from apps.remote.models import RemoteNegativeCache, RemoteQuery, RemoteQueryItem, RemoteTask
from apps.remote.provider import ProtocolProviderError
from apps.remote.worker import RemoteWorker

pytestmark = pytest.mark.django_db(transaction=True)

IK = "RYYVLZVUVIJVGH-UHFFFAOYSA-N"
COMPOUND_ID = "Q12975518-2"


def _node(numeric_id: int, name: str) -> ChemOntNode:
    return ChemOntNode.objects.get_or_create(
        numeric_id=numeric_id,
        defaults={
            "chemont_id": f"CHEMONT:{numeric_id:07d}",
            "name": name,
            "ontology_version": "2.1",
        },
    )[0]


def _csv_bytes(compound_id: str = COMPOUND_ID) -> bytes:
    return (
        "CompoundID,ChemOntID,ClassifiedResults\n"
        f"{compound_id},CHEMONTID:0000000,Kingdom: Organic compounds\n"
        f"{compound_id},CHEMONTID:0000012,Superclass: Lipids and lipid-like molecules\n"
        f"{compound_id},CHEMONTID:0000259,Class: Prenol lipids\n"
        f"{compound_id},CHEMONTID:0001550,Subclass: Sesquiterpenoids\n"
        f"{compound_id},CHEMONTID:0001550,Direct_parent: Sesquiterpenoids\n"
        f"{compound_id},CHEMONTID:0001201,alternative_parents: Thiocyanates\n"
        f"{compound_id},NotAvailable,\"Substituents: [\"\"Sesquiterpenoid\"\", \"\"Thiocyanate\"\"]\"\n"
        f"{compound_id},NotAvailable,\"Ancestors: [\"\"Chemical entities\"\", \"\"Prenol lipids\"\"]\"\n"
        f"{compound_id},NotAvailable,Classification_version: 2.1\n"
    ).encode()


def test_parse_classyfire_web_csv_uses_identifier_mapping_and_normalizes_slots():
    from apps.classifications.classyfire_web_csv import parse_classyfire_web_csv

    records = parse_classyfire_web_csv(_csv_bytes(), {COMPOUND_ID: IK})

    assert len(records) == 1
    norm = records[0]
    assert norm.inchikey == IK
    assert norm.kingdom is not None
    assert norm.kingdom.chemont_id == "CHEMONT:0000000"
    assert norm.kingdom.name == "Organic compounds"
    assert norm.superclass is not None
    assert norm.superclass.chemont_id == "CHEMONT:0000012"
    assert norm.class_ is not None
    assert norm.class_.name == "Prenol lipids"
    assert norm.subclass is not None
    assert norm.subclass.name == "Sesquiterpenoids"
    assert norm.direct_parent is not None
    assert norm.direct_parent.chemont_id == "CHEMONT:0001550"
    assert norm.classification_version == "2.1"
    assert norm.alternative_parents == [
        {"chemont_id": "CHEMONT:0001201", "name": "Thiocyanates"}
    ]
    assert norm.raw_json is not None
    assert norm.raw_json["compound_id"] == COMPOUND_ID
    assert norm.raw_json["inchikey"] == IK
    assert norm.raw_json["substituents"] == ["Sesquiterpenoid", "Thiocyanate"]


def test_import_classyfire_web_csv_records_artifact_advances_live_and_resolves_negative(
    settings, tmp_path
):
    from apps.classifications.classyfire_web_csv import import_classyfire_web_csv_artifact
    from apps.remote.negative_cache import record_negative_failure

    settings.SOURCE_ARTIFACT_DIR = str(tmp_path)
    for numeric_id, name in [
        (0, "Organic compounds"),
        (12, "Lipids and lipid-like molecules"),
        (259, "Prenol lipids"),
        (1550, "Sesquiterpenoids"),
        (1201, "Thiocyanates"),
    ]:
        _node(numeric_id, name)
    for _ in range(3):
        record_negative_failure(
            IK,
            operation="POLL",
            http_status=500,
            detail="ClassyFire cache server failed",
        )

    raw = _csv_bytes()
    result = import_classyfire_web_csv_artifact(
        raw,
        identifier_to_inchikey={COMPOUND_ID: IK},
        name="classyfire-web.csv",
        uri="https://classyfire.wishartlab.com/queries/Q12975518.csv",
    )

    artifact = SourceArtifact.objects.get(pk=result.artifact_id)
    assert artifact.source_type == SourceType.WISHART_LIVE_QUERY
    assert artifact.checksum == hashlib.sha256(raw).hexdigest()
    assert artifact.byte_size == len(raw)
    assert artifact.metadata["provider"] == "wishart-classyfire-web-csv"

    assert result.record_count == 1
    assert result.outcomes[0].applied is True
    event = ClassificationResult.objects.get(pk=result.outcomes[0].result_id)
    assert event.kind == ResultKind.APPLIED
    assert event.source_type == SourceType.WISHART_LIVE_QUERY
    assert event.source_artifact_id == artifact.pk
    assert event.classification_version == "2.1"
    assert event.raw_json["compound_id"] == COMPOUND_ID
    assert event.raw_json["rows"][0]["ChemOntID"] == "CHEMONTID:0000000"
    assert event.kingdom_id == 0
    assert event.direct_parent_id == 1550

    live = ClassyfireLive.objects.get(inchikey=IK)
    assert live.result_id == event.pk
    assert live.source_type == SourceType.WISHART_LIVE_QUERY
    negative = RemoteNegativeCache.objects.get(inchikey=IK)
    assert negative.state == RemoteNegativeCache.State.RESOLVED


def test_classyfire_web_csv_rejects_unknown_identifier_before_persisting(settings, tmp_path):
    from apps.classifications.classyfire_web_csv import (
        ClassyfireWebCsvImportError,
        import_classyfire_web_csv_artifact,
    )

    settings.SOURCE_ARTIFACT_DIR = str(tmp_path)

    with pytest.raises(ClassyfireWebCsvImportError, match="no InChIKey mapping"):
        import_classyfire_web_csv_artifact(
            _csv_bytes("UNKNOWN-ID"),
            identifier_to_inchikey={COMPOUND_ID: IK},
            name="bad.csv",
        )

    assert SourceArtifact.objects.count() == 0
    assert ClassificationResult.objects.count() == 0
    assert ClassyfireLive.objects.count() == 0


def test_import_classyfire_web_csv_command_imports_mapping_file(settings, tmp_path):
    settings.SOURCE_ARTIFACT_DIR = str(tmp_path / "artifacts")
    _node(0, "Organic compounds")
    csv_path = tmp_path / "classyfire-web.csv"
    mapping_path = tmp_path / "mapping.json"
    csv_path.write_bytes(_csv_bytes())
    mapping_path.write_text(json.dumps({COMPOUND_ID: IK}), encoding="utf-8")

    call_command(
        "import_classyfire_web_csv",
        str(csv_path),
        "--mapping",
        str(mapping_path),
        verbosity=0,
    )

    live = ClassyfireLive.objects.get(inchikey=IK)
    assert live.source_type == SourceType.WISHART_LIVE_QUERY
    artifact = SourceArtifact.objects.get()
    assert artifact.name == "classyfire-web.csv"
    assert artifact.metadata["provider"] == "wishart-classyfire-web-csv"


def test_worker_uses_query_csv_when_completed_json_is_unparseable(settings, tmp_path):
    settings.SOURCE_ARTIFACT_DIR = str(tmp_path / "artifacts")
    _node(0, "Organic compounds")
    job = BatchJob.objects.create(secret_hash="secret", total_count=1)
    BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=IK,
        inchikey=IK,
        status=BatchItemStatus.PENDING_CLASSIFICATION,
    )
    RemoteTask.objects.create(inchikey=IK, state=TaskState.READY)
    claimed = queue.claim_task("csv-fallback-worker")
    assert claimed is not None
    claim = queue.claim_from_task(claimed)
    query = RemoteQuery.objects.create(
        remote_query_id="QCSV",
        payload_hash="payload",
        state=QueryState.SUBMITTED,
    )
    query_item = RemoteQueryItem.objects.create(query=query, task=claimed, inchikey=IK)

    class CsvFallbackProvider:
        def parse_results(self, raw_json, *, max_entities=None):
            raise ProtocolProviderError("json export failed")

        def download_query_csv(self, remote_query_id: str) -> bytes:
            assert remote_query_id == "QCSV"
            return _csv_bytes(IK)

    worker = RemoteWorker(CsvFallbackProvider(), worker_id="csv-fallback-worker")

    worker._apply_query_result(claim, claimed, query, raw={"entities": []})

    live = ClassyfireLive.objects.get(inchikey=IK)
    assert live.source_type == SourceType.WISHART_LIVE_QUERY
    item = BatchItem.objects.get(job=job)
    assert item.status == BatchItemStatus.COMPLETE_AFTER_WISHART
    assert item.resolved_classification_id == live.result_id
    claimed.refresh_from_db()
    assert claimed.state == TaskState.DONE
    query.refresh_from_db()
    assert query.state == QueryState.COMPLETE
    query_item.refresh_from_db()
    assert query_item.apply_state == RemoteQueryItemApplyState.APPLIED
