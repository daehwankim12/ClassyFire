from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

IK_A = "RDHQFKQIGNGIED-MRVPVSSYSA-N"
IK_B = "WQZGKKKJIJFFOK-GASJEMHNSA-N"


@pytest.mark.django_db(transaction=True)
def test_metrics_snapshot_reports_queue_lag_provider_and_artifacts(tmp_path, settings):
    from apps.batches.models import BatchJob, ExportArtifact
    from apps.classifications.models import ClassificationResult
    from apps.core.enums import ResultKind, SourceType, TaskState
    from apps.remote.models import (
        RemoteAttempt,
        RemoteRateBucket,
        RemoteTask,
    )

    settings.BACKUP_DATA_PATH = str(tmp_path)
    now = timezone.now()
    RemoteTask.objects.create(
        inchikey=IK_A,
        state=TaskState.READY,
        next_run_at=now - timedelta(seconds=30),
    )
    RemoteTask.objects.create(inchikey=IK_B, state=TaskState.RETRY_WAIT)
    RemoteRateBucket.objects.create(
        provider="wishart",
        bucket="entity",
        capacity=60,
        tokens=2,
        refill_rate_per_sec=1,
        blocked_until=now + timedelta(seconds=10),
    )
    RemoteAttempt.objects.create(
        provider="wishart",
        operation=RemoteAttempt.OPERATION_ENTITY,
        classification="RETRYABLE",
        http_status=503,
    )
    ClassificationResult.objects.create(
        requested_inchikey=IK_A,
        returned_inchikey=IK_B,
        kind=ResultKind.REJECTED_MISMATCH,
        source_type=SourceType.WISHART_LIVE_ENTITY,
        source_priority=100,
        raw_json={"bad": "key"},
        raw_checksum="0" * 64,
        retrieved_at=now,
    )
    job = BatchJob.objects.create(secret_hash="x")
    ExportArtifact.objects.create(
        job=job,
        fmt="csv",
        token="tok-metrics",
        path=str(tmp_path / "tok-metrics.csv"),
        checksum="1" * 64,
        byte_size=123,
        rows_signature="2" * 64,
        row_count=5,
        job_state_signature="3" * 64,
        expires_at=now + timedelta(days=1),
    )

    from apps.operations.metrics import collect_metrics_snapshot

    snapshot = collect_metrics_snapshot(data_path=str(tmp_path))
    payload = snapshot.to_dict()

    assert payload["remote_queue"]["by_state"]["READY"] == 1
    assert payload["remote_queue"]["by_state"]["RETRY_WAIT"] == 1
    assert payload["remote_queue"]["due_count"] == 1
    assert payload["remote_queue"]["max_due_lag_seconds"] >= 30
    assert "circuits" not in payload["provider"]
    assert payload["provider"]["rate_buckets"]["wishart:entity"]["blocked"] is True
    assert payload["provider"]["attempts_by_classification"]["RETRYABLE"] == 1
    assert payload["classification"]["mismatch_count"] == 1
    assert payload["exports"]["artifact_count"] == 1
    assert payload["exports"]["total_bytes"] == 123


@pytest.mark.django_db(transaction=True)
def test_emit_metrics_snapshot_command_outputs_json(capsys, tmp_path, settings):
    settings.BACKUP_DATA_PATH = str(tmp_path)

    call_command("emit_metrics_snapshot", "--json")
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert "remote_queue" in payload
    assert "provider" in payload
    assert "classification" in payload
    assert "exports" in payload
