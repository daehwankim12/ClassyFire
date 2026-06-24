from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db.models import Count, Min, Sum
from django.utils import timezone


@dataclass(frozen=True)
class MetricsSnapshot:
    generated_at: str
    remote_queue: dict[str, Any]
    provider: dict[str, Any]
    classification: dict[str, Any]
    exports: dict[str, Any]
    disk: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "remote_queue": self.remote_queue,
            "provider": self.provider,
            "classification": self.classification,
            "exports": self.exports,
            "disk": self.disk,
        }


def _remote_queue_metrics(now) -> dict[str, Any]:
    from apps.core.enums import TaskState
    from apps.remote.models import RemoteTask

    by_state = {
        row["state"]: int(row["count"])
        for row in RemoteTask.objects.values("state").annotate(count=Count("pk"))
    }
    due = RemoteTask.objects.exclude(state=TaskState.DONE).filter(
        next_run_at__isnull=False,
        next_run_at__lte=now,
    )
    due_summary = due.aggregate(count=Count("pk"), oldest=Min("next_run_at"))
    oldest = due_summary["oldest"]
    max_lag = 0
    if oldest is not None:
        max_lag = max(0, int((now - oldest).total_seconds()))

    active_count = (
        RemoteTask.objects.exclude(state=TaskState.DONE)
        .values("inchikey")
        .distinct()
        .count()
    )
    return {
        "by_state": by_state,
        "active_unique_inchikey_count": active_count,
        "due_count": int(due_summary["count"] or 0),
        "max_due_lag_seconds": max_lag,
    }


def _provider_metrics(now) -> dict[str, Any]:
    from apps.remote.models import RemoteAttempt, RemoteCircuit, RemoteRateBucket

    attempts = {
        row["classification"]: int(row["count"])
        for row in (
            RemoteAttempt.objects.values("classification")
            .annotate(count=Count("pk"))
            .order_by()
        )
    }
    circuits = {
        circuit.provider: {
            "state": circuit.state,
            "consecutive_failures": circuit.consecutive_failures,
            "opened_at": circuit.opened_at.isoformat() if circuit.opened_at else None,
        }
        for circuit in RemoteCircuit.objects.all()
    }
    buckets = {}
    for bucket in RemoteRateBucket.objects.all():
        blocked_until = bucket.blocked_until
        buckets[f"{bucket.provider}:{bucket.bucket}"] = {
            "capacity": bucket.capacity,
            "tokens": bucket.tokens,
            "refill_rate_per_sec": bucket.refill_rate_per_sec,
            "blocked": blocked_until is not None and blocked_until > now,
            "blocked_until": blocked_until.isoformat() if blocked_until else None,
        }

    return {
        "attempts_by_classification": attempts,
        "circuits": circuits,
        "rate_buckets": buckets,
    }


def _classification_metrics() -> dict[str, Any]:
    from apps.classifications.models import ClassificationResult
    from apps.core.enums import ResultKind

    by_kind = {
        row["kind"]: int(row["count"])
        for row in (
            ClassificationResult.objects.values("kind")
            .annotate(count=Count("pk"))
            .order_by()
        )
    }
    return {
        "results_by_kind": by_kind,
        "mismatch_count": by_kind.get(ResultKind.REJECTED_MISMATCH, 0),
    }


def _export_metrics() -> dict[str, Any]:
    from apps.batches.models import ExportArtifact

    summary = ExportArtifact.objects.aggregate(
        artifact_count=Count("pk"),
        total_bytes=Sum("byte_size"),
        total_rows=Sum("row_count"),
    )
    return {
        "artifact_count": int(summary["artifact_count"] or 0),
        "total_bytes": int(summary["total_bytes"] or 0),
        "total_rows": int(summary["total_rows"] or 0),
    }


def _disk_metrics(data_path: str | None) -> dict[str, Any]:
    configured_path = data_path or str(getattr(settings, "BACKUP_DATA_PATH", "/") or "/")
    path = Path(configured_path)
    usage = shutil.disk_usage(path)
    percent = (usage.used / usage.total * 100) if usage.total else 0.0
    return {
        "path": str(path),
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percent_used": round(percent, 2),
    }


def collect_metrics_snapshot(data_path: str | None = None) -> MetricsSnapshot:
    now = timezone.now()
    return MetricsSnapshot(
        generated_at=now.isoformat(),
        remote_queue=_remote_queue_metrics(now),
        provider=_provider_metrics(now),
        classification=_classification_metrics(),
        exports=_export_metrics(),
        disk=_disk_metrics(data_path),
    )
