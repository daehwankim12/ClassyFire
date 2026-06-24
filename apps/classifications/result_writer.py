"""Write-time persistence of a remote ClassyFire result into classyfire_live.

This module is the single place that *writes* a live classification row. It is
called by the remote worker's fan-out, but it deliberately lives under
``apps/classifications`` (next to the read-side selectors/services) because it
owns the write-time precedence + history-archival invariants of the live table.

Write-time precedence (language-agnostic, mirrors docs/state-machines.md §5):

* ``classyfire_live`` is UNIQUE per inchikey.
* A new result is applied only when its ``source_priority`` is ``>=`` the stored
  row's priority (live=100 never gets overwritten by a lower-trust seed/archive;
  a newer live result at equal priority supersedes the old one).
* When a new result supersedes an existing row, the old row is snapshotted into
  ``classification_history`` first, then the live row is upserted in place.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.classifications.models import ClassificationHistory, ClassyfireLive
from apps.core.enums import SOURCE_PRIORITY, SourceType
from apps.remote.provider import ChemOntSlot, NormalizedClassification


@dataclass(frozen=True)
class WriteOutcome:
    """Result of attempting to persist a classification."""

    applied: bool
    live_id: int | None
    archived_history: bool
    reason: str | None = None


def _resolve_node_id(slot: ChemOntSlot | None) -> int | None:
    """Resolve a ChemOnt slot to its numeric_id FK, or None if unknown.

    Matches by chemont_id (the stable textual key) first, then by name. A
    missing/unknown node yields None rather than failing the whole write — the
    raw_json always retains the full provenance.
    """
    if slot is None:
        return None
    from apps.chemont.models import ChemOntNode

    if slot.chemont_id:
        node = ChemOntNode.objects.filter(chemont_id=slot.chemont_id).values_list(
            "numeric_id", flat=True
        ).first()
        if node is not None:
            return int(node)
    if slot.name:
        node = ChemOntNode.objects.filter(name=slot.name).values_list(
            "numeric_id", flat=True
        ).first()
        if node is not None:
            return int(node)
    return None


def _labels_payload(norm: NormalizedClassification) -> dict[str, object]:
    def lab(slot: ChemOntSlot | None) -> dict[str, str | None] | None:
        if slot is None:
            return None
        return {"chemont_id": slot.chemont_id, "name": slot.name}

    return {
        "kingdom": lab(norm.kingdom),
        "superclass": lab(norm.superclass),
        "class": lab(norm.class_),
        "subclass": lab(norm.subclass),
        "direct_parent": lab(norm.direct_parent),
        "alternative_parents": norm.alternative_parents,
    }


def _snapshot(row: ClassyfireLive) -> ClassificationHistory:
    return ClassificationHistory(
        inchikey=row.inchikey,
        source_type=row.source_type,
        source_priority=row.source_priority,
        retrieved_at=row.retrieved_at,
        payload={
            "source_type": row.source_type,
            "source_priority": row.source_priority,
            "kingdom_id": row.kingdom_id,
            "superclass_id": row.superclass_id,
            "class_id": row.class_node_id,
            "subclass_id": row.subclass_id,
            "direct_parent_id": row.direct_parent_id,
            "labels": row.labels,
            "raw_json": row.raw_json,
            "remote_query_id": row.remote_query_id,
            "source_ref": row.source_ref,
            "classification_version": row.classification_version,
            "retrieved_at": row.retrieved_at.isoformat() if row.retrieved_at else None,
        },
    )


@transaction.atomic
def write_live_result(
    inchikey: str,
    norm: NormalizedClassification,
    *,
    source_type: SourceType,
    remote_query_id: str | None = None,
    source_ref: str | None = None,
    retrieved_at: datetime | None = None,
) -> WriteOutcome:
    """Upsert a live classification, archiving any superseded row to history.

    Returns a :class:`WriteOutcome` describing whether the write was applied.
    """
    priority = SOURCE_PRIORITY[source_type]
    retrieved_at = retrieved_at or timezone.now()

    values = {
        "source_type": source_type,
        "source_priority": priority,
        "kingdom_id": _resolve_node_id(norm.kingdom),
        "superclass_id": _resolve_node_id(norm.superclass),
        "class_node_id": _resolve_node_id(norm.class_),
        "subclass_id": _resolve_node_id(norm.subclass),
        "direct_parent_id": _resolve_node_id(norm.direct_parent),
        "labels": _labels_payload(norm),
        "raw_json": norm.raw_json,
        "remote_query_id": remote_query_id,
        "source_ref": source_ref,
        "classification_version": norm.classification_version,
        "retrieved_at": retrieved_at,
    }

    existing = (
        ClassyfireLive.objects.select_for_update()
        .filter(inchikey=inchikey)
        .first()
    )

    if existing is None:
        # No row yet — try to claim the unique slot with a create, guarded by a
        # savepoint. If a concurrent writer wins the create race the unique index
        # raises IntegrityError; we roll back the savepoint (not the outer
        # transaction) and fall through to the supersede path with the row now
        # locked via select_for_update — no IntegrityError escapes and no
        # no-row window is ever observed.
        try:
            with transaction.atomic():
                row = ClassyfireLive.objects.create(inchikey=inchikey, **values)
            return WriteOutcome(applied=True, live_id=row.id, archived_history=False)
        except IntegrityError:
            existing = (
                ClassyfireLive.objects.select_for_update()
                .filter(inchikey=inchikey)
                .first()
            )

    if existing is None:
        # Should not happen (we just hit a unique violation) but stay defensive.
        return WriteOutcome(
            applied=False, live_id=None, archived_history=False, reason="lost row"
        )

    if existing.source_priority > priority:
        # Stored row is strictly higher-trust: keep it (live never clobbered by
        # a lower-priority result).
        return WriteOutcome(
            applied=False,
            live_id=existing.id,
            archived_history=False,
            reason="existing higher priority",
        )

    if existing.source_priority == priority and existing.retrieved_at >= retrieved_at:
        return WriteOutcome(
            applied=False,
            live_id=existing.id,
            archived_history=False,
            reason="existing same priority is newer or equal",
        )

    # >= priority: the new result supersedes. Snapshot the prior row to history
    # FIRST, then update the live row in place (no delete+create window).
    _snapshot(existing).save()
    for field, value in values.items():
        setattr(existing, field, value)
    existing.save(update_fields=list(values.keys()))
    return WriteOutcome(applied=True, live_id=existing.id, archived_history=True)
