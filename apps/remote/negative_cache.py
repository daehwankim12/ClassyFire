from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.classifications.services import ChemOntLabel, ClassificationLookupResult
from apps.remote.models import ManualClassificationOverride, RemoteNegativeCache

ADMIN_MANUAL_FALLBACK_SOURCE = "ADMIN_MANUAL_FALLBACK"
ADMIN_MANUAL_FALLBACK_PRIORITY = 95
NEGATIVE_HARD_FAILED_MESSAGE = (
    "Wishart negative cache hard-failed for this InChIKey."
)
NEGATIVE_FAILURE_THRESHOLD = 3


def record_negative_failure(
    inchikey: str,
    *,
    operation: str,
    http_status: int | None,
    detail: str | None,
) -> RemoteNegativeCache:
    """Record a failed Wishart submit/poll path for one InChIKey."""
    now = timezone.now()
    with transaction.atomic():
        cache, _ = RemoteNegativeCache.objects.select_for_update().get_or_create(
            inchikey=inchikey,
            defaults={
                "state": RemoteNegativeCache.State.CANDIDATE,
                "failure_count": 0,
            },
        )
        if cache.state == RemoteNegativeCache.State.RESOLVED:
            cache.failure_count = 0
            cache.state = RemoteNegativeCache.State.CANDIDATE
        cache.failure_count += 1
        cache.last_failure_operation = operation
        cache.last_http_status = http_status
        cache.last_detail = detail
        cache.last_failed_at = now
        cache.next_recheck_at = now
        if cache.failure_count >= NEGATIVE_FAILURE_THRESHOLD:
            cache.state = RemoteNegativeCache.State.HARD_FAILED
        cache.save(
            update_fields=[
                "state",
                "failure_count",
                "last_failure_operation",
                "last_http_status",
                "last_detail",
                "last_failed_at",
                "next_recheck_at",
                "updated_at",
            ]
        )
        return cache


def create_manual_fallback(
    negative_cache: RemoteNegativeCache,
    *,
    labels: dict[str, Any],
    actor: str,
    admin_note: str | None = None,
    raw_payload: dict[str, Any] | None = None,
    classification_version: str | None = None,
    source_ref: str | None = None,
) -> ManualClassificationOverride:
    """Create or replace admin fallback data for a negative-cache row."""
    if negative_cache.state != RemoteNegativeCache.State.HARD_FAILED:
        raise ValueError("manual fallback is allowed only for HARD_FAILED rows")
    override, _ = ManualClassificationOverride.objects.update_or_create(
        inchikey=negative_cache.inchikey,
        defaults={
            "negative_cache": negative_cache,
            "labels": labels,
            "raw_payload": raw_payload,
            "classification_version": classification_version,
            "source_ref": source_ref,
            "actor": actor,
            "admin_note": admin_note,
        },
    )
    return override


def mark_negative_resolved_from_wishart(inchikey: str) -> None:
    """Close negative state after a verified Wishart result and delete fallback."""
    from apps.batches.models import BatchItem
    from apps.batches.services import recompute_job_counters
    from apps.classifications.services import (
        classification_lookup_snapshot,
        lookup_current_classification,
    )
    from apps.core.enums import BatchItemStatus

    result = lookup_current_classification(inchikey)
    if result is None:
        return
    snapshot = classification_lookup_snapshot(result)
    with transaction.atomic():
        affected_jobs = []
        if snapshot is not None:
            affected_jobs = list(
                BatchItem.objects.filter(
                    inchikey=inchikey,
                    classification_snapshot__source_type=ADMIN_MANUAL_FALLBACK_SOURCE,
                )
                .values_list("job_id", flat=True)
                .distinct()
            )
            BatchItem.objects.filter(
                inchikey=inchikey,
                classification_snapshot__source_type=ADMIN_MANUAL_FALLBACK_SOURCE,
            ).update(
                status=BatchItemStatus.FOUND_LIVE_CACHE,
                classification_snapshot=snapshot,
                resolved_classification_id=snapshot.get("result_id"),
                message=None,
            )
        RemoteNegativeCache.objects.filter(inchikey=inchikey).update(
            state=RemoteNegativeCache.State.RESOLVED,
            next_recheck_at=None,
            updated_at=timezone.now(),
        )
        ManualClassificationOverride.objects.filter(inchikey=inchikey).delete()
        for job_id in affected_jobs:
            recompute_job_counters(job_id)


def reset_negative_for_recheck(
    negative_cache: RemoteNegativeCache, *, actor: str | None = None
) -> RemoteNegativeCache:
    """Allow operators to force a fresh Wishart recheck for a hard failure."""
    negative_cache.state = RemoteNegativeCache.State.CANDIDATE
    negative_cache.failure_count = 0
    negative_cache.next_recheck_at = timezone.now()
    negative_cache.last_detail = (
        f"manual recheck requested by {actor}" if actor else "manual recheck requested"
    )
    negative_cache.save(
        update_fields=[
            "state",
            "failure_count",
            "next_recheck_at",
            "last_detail",
            "updated_at",
        ]
    )
    return negative_cache


def hard_negative_inchikeys(inchikeys: Iterable[str]) -> set[str]:
    keys = list(dict.fromkeys(inchikeys))
    if not keys:
        return set()
    return set(
        RemoteNegativeCache.objects.filter(
            inchikey__in=keys,
            state=RemoteNegativeCache.State.HARD_FAILED,
        ).values_list("inchikey", flat=True)
    )


def lookup_manual_fallback(inchikey: str) -> ClassificationLookupResult | None:
    override = ManualClassificationOverride.objects.filter(inchikey=inchikey).first()
    if override is None:
        return None
    return _override_to_lookup_result(override)


def lookup_manual_fallbacks(
    inchikeys: Iterable[str],
) -> dict[str, ClassificationLookupResult]:
    keys = list(dict.fromkeys(inchikeys))
    if not keys:
        return {}
    return {
        override.inchikey: _override_to_lookup_result(override)
        for override in ManualClassificationOverride.objects.filter(inchikey__in=keys)
    }


def _label(labels: dict[str, Any], key: str) -> ChemOntLabel | None:
    value = labels.get(key)
    if not isinstance(value, dict):
        return None
    chemont_id = value.get("chemont_id")
    name = value.get("name")
    if chemont_id is None and name is None:
        return None
    return ChemOntLabel(
        chemont_id=str(chemont_id) if chemont_id is not None else None,
        name=str(name) if name is not None else None,
    )


def _override_to_lookup_result(
    override: ManualClassificationOverride,
) -> ClassificationLookupResult:
    labels = override.labels if isinstance(override.labels, dict) else {}
    return ClassificationLookupResult(
        inchikey=override.inchikey,
        kingdom=_label(labels, "kingdom"),
        superclass=_label(labels, "superclass"),
        class_=_label(labels, "class"),
        subclass=_label(labels, "subclass"),
        direct_parent=_label(labels, "direct_parent"),
        source_type=ADMIN_MANUAL_FALLBACK_SOURCE,
        source_priority=ADMIN_MANUAL_FALLBACK_PRIORITY,
        source_ref=override.source_ref,
        classification_version=override.classification_version,
        retrieved_at=override.updated_at,
        query_id=None,
        result_id=None,
    )
