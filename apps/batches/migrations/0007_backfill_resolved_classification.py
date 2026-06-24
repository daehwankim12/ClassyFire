"""Backfill BatchItem.resolved_classification from existing snapshots.

For each resolved BatchItem that has a pinned ``classification_snapshot`` but no
``resolved_classification`` FK yet, link an immutable ClassificationResult event:

1. If the snapshot carries a ``result_id`` for an existing event, link it.
2. Else if the current ``classyfire_live`` row for the item's InChIKey already
   points at an event (``result_id``), link that.
3. Else reconstruct a SYNTHETIC immutable event from the snapshot JSON
   (kind=APPLIED, raw_json=snapshot) so a resolved item's FK is never silently
   null. This preserves provenance for pre-event-log rows.

The dev/test DB is empty, so this mostly matters for shape correctness, but it is
written to run safely against populated data. Reverse is a no-op (we never delete
immutable events nor unset a pinned FK).
"""
from __future__ import annotations

import hashlib
import json

from django.db import migrations
from django.utils import timezone

_FOUND_STATUSES = (
    "FOUND_LIVE_CACHE",
    "FOUND_SEED_CACHE",
    "FOUND_WISHART_ENTITY",
    "COMPLETE_AFTER_WISHART",
)


def _checksum(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_labels(snapshot: dict) -> dict:
    return {
        "kingdom": snapshot.get("kingdom"),
        "superclass": snapshot.get("superclass"),
        "class": snapshot.get("class"),
        "subclass": snapshot.get("subclass"),
        "direct_parent": snapshot.get("direct_parent"),
        "alternative_parents": [],
    }


def backfill(apps, schema_editor):
    BatchItem = apps.get_model("batches", "BatchItem")
    ClassyfireLive = apps.get_model("classifications", "ClassyfireLive")
    ClassificationResult = apps.get_model(
        "classifications", "ClassificationResult"
    )

    items = BatchItem.objects.filter(
        status__in=_FOUND_STATUSES,
        resolved_classification__isnull=True,
        classification_snapshot__isnull=False,
    )
    for item in items.iterator(chunk_size=500):
        snapshot = item.classification_snapshot
        if not isinstance(snapshot, dict):
            continue

        # 1. Snapshot already references an event.
        result_id = snapshot.get("result_id")
        if result_id is not None and ClassificationResult.objects.filter(
            pk=result_id
        ).exists():
            item.resolved_classification_id = result_id
            item.save(update_fields=["resolved_classification"])
            continue

        # 2. Current live row points at an event.
        if item.inchikey:
            live_result_id = (
                ClassyfireLive.objects.filter(inchikey=item.inchikey)
                .values_list("result_id", flat=True)
                .first()
            )
            if live_result_id is not None:
                item.resolved_classification_id = live_result_id
                item.save(update_fields=["resolved_classification"])
                continue

        # 3. Reconstruct a synthetic immutable event from the snapshot JSON.
        if not item.inchikey:
            continue
        source_type = snapshot.get("source_type") or "HISTORICAL_AGGREGATE"
        source_priority = snapshot.get("source_priority")
        if not isinstance(source_priority, int):
            source_priority = 0
        event = ClassificationResult.objects.create(
            requested_inchikey=item.inchikey,
            returned_inchikey=item.inchikey,
            kind="APPLIED",
            source_type=source_type,
            source_priority=source_priority,
            labels=_snapshot_labels(snapshot),
            raw_json=snapshot,
            raw_checksum=_checksum(snapshot),
            source_ref=snapshot.get("source_ref"),
            remote_query_id=snapshot.get("query_id"),
            classification_version=snapshot.get("classification_version"),
            retrieved_at=timezone.now(),
        )
        item.resolved_classification_id = event.pk
        item.save(update_fields=["resolved_classification"])


def noop_reverse(apps, schema_editor):
    """Immutable events are never deleted; pinned FKs are not unset."""


class Migration(migrations.Migration):

    dependencies = [
        ("batches", "0006_batchitem_resolved_classification"),
    ]

    operations = [
        migrations.RunPython(backfill, noop_reverse),
    ]
