"""Write-time persistence of a remote ClassyFire result into classyfire_live.

This module is the single place that *writes* a live classification row. It is
called by the remote worker's fan-out, but it deliberately lives under
``apps/classifications`` (next to the read-side selectors/services) because it
owns the write-time precedence + history-archival invariants of the live table.

Append-only event log (F-05/F-20)
---------------------------------
EVERY call to :func:`write_live_result` appends exactly one immutable
:class:`ClassificationResult` event (one INSERT, never UPDATE/DELETE) — including
mismatches and rejected/lower-priority/stale writes. The event's ``kind`` records
its disposition relative to the live pointer at write time. Only when the result
is *applied* or *supersedes* does the live row advance in place and its
``result`` FK point at the new event.

Write-time precedence (language-agnostic, mirrors docs/state-machines.md §5):

* ``classyfire_live`` is UNIQUE per inchikey.
* A new result is applied only when its ``source_priority`` is ``>=`` the stored
  row's priority (live=100 never gets overwritten by a lower-trust seed/archive;
  a newer live result at equal priority supersedes the old one).
* When a new result supersedes an existing row, the old row is snapshotted into
  ``classification_history`` first, then the live row is upserted in place AND
  its ``result`` FK is repointed at the freshly-appended event.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.classifications.models import (
    ClassificationHistory,
    ClassificationResult,
    ClassyfireLive,
    SourceArtifact,
)
from apps.core.enums import SOURCE_PRIORITY, ResultKind, SourceType
from apps.remote.provider import ChemOntSlot, NormalizedClassification


@dataclass(frozen=True)
class WriteOutcome:
    """Result of attempting to persist a classification.

    ``result_id`` is the PK of the immutable :class:`ClassificationResult` event
    appended for this write (always set — every write appends exactly one event,
    even rejected/mismatch writes). ``applied``/``live_id``/``archived_history``
    keep their historical meaning so existing callers/tests are unaffected.
    """

    applied: bool
    live_id: int | None
    archived_history: bool
    reason: str | None = None
    result_id: int | None = None


def raw_checksum(raw_json: object) -> str:
    """SHA-256 hex of the canonical JSON encoding of ``raw_json`` (provenance).

    A stable, sorted, separator-fixed encoding so the same payload always yields
    the same digest across languages/writers. ``None`` hashes the literal
    ``null`` so a missing payload still gets a well-defined checksum.
    """
    encoded = json.dumps(
        raw_json,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolve_node_id(slot: ChemOntSlot | None) -> int | None:
    """Resolve a ChemOnt slot to its numeric_id FK, or None if unknown.

    Matches only by chemont_id, the stable textual key. A missing/unknown node
    yields None rather than falling back to non-unique display names; raw_json
    and labels always retain the full upstream provenance.
    """
    if slot is None:
        return None
    from apps.chemont.models import ChemOntNode

    if not slot.chemont_id:
        return None
    node = ChemOntNode.objects.filter(chemont_id=slot.chemont_id).values_list(
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
            "result_id": row.result_id,
            "retrieved_at": row.retrieved_at.isoformat() if row.retrieved_at else None,
        },
    )


def _append_event(
    *,
    requested_inchikey: str,
    returned_inchikey: str | None,
    kind: ResultKind,
    source_type: SourceType,
    priority: int,
    norm: NormalizedClassification,
    remote_query_id: str | None,
    source_ref: str | None,
    classification_version: str | None,
    retrieved_at: datetime,
    source_artifact: SourceArtifact | None,
) -> ClassificationResult:
    """Append ONE immutable classification event. Never updated/deleted."""
    return ClassificationResult.objects.create(
        requested_inchikey=requested_inchikey,
        returned_inchikey=returned_inchikey,
        kind=kind,
        source_type=source_type,
        source_priority=priority,
        kingdom_id=_resolve_node_id(norm.kingdom),
        superclass_id=_resolve_node_id(norm.superclass),
        class_node_id=_resolve_node_id(norm.class_),
        subclass_id=_resolve_node_id(norm.subclass),
        direct_parent_id=_resolve_node_id(norm.direct_parent),
        labels=_labels_payload(norm),
        raw_json=norm.raw_json,
        raw_checksum=raw_checksum(norm.raw_json),
        source_ref=source_ref,
        remote_query_id=remote_query_id,
        classification_version=classification_version,
        source_artifact=source_artifact,
        retrieved_at=retrieved_at,
    )


def _apply_event_to_live(live: ClassyfireLive, event: ClassificationResult) -> None:
    """Copy an applied event's classification columns onto the live row + repoint FK."""
    live.source_type = event.source_type
    live.source_priority = event.source_priority
    live.kingdom_id = event.kingdom_id
    live.superclass_id = event.superclass_id
    live.class_node_id = event.class_node_id
    live.subclass_id = event.subclass_id
    live.direct_parent_id = event.direct_parent_id
    live.labels = event.labels
    live.raw_json = event.raw_json
    live.remote_query_id = event.remote_query_id
    live.source_ref = event.source_ref
    live.classification_version = event.classification_version
    live.retrieved_at = event.retrieved_at
    live.result = event


_LIVE_FIELDS = [
    "source_type",
    "source_priority",
    "kingdom_id",
    "superclass_id",
    "class_node_id",
    "subclass_id",
    "direct_parent_id",
    "labels",
    "raw_json",
    "remote_query_id",
    "source_ref",
    "classification_version",
    "retrieved_at",
    "result",
]


@transaction.atomic
def write_live_result(
    inchikey: str,
    norm: NormalizedClassification,
    *,
    source_type: SourceType,
    remote_query_id: str | None = None,
    source_ref: str | None = None,
    retrieved_at: datetime | None = None,
    source_artifact: SourceArtifact | None = None,
) -> WriteOutcome:
    """Append an immutable event, then upsert the live pointer when applied.

    ALWAYS appends exactly one :class:`ClassificationResult` event first (one
    INSERT, never UPDATE/DELETE). For a key mismatch the event is recorded with
    ``kind = REJECTED_MISMATCH`` and live is left unchanged. Otherwise precedence
    decides the ``kind`` (APPLIED/SUPERSEDED vs REJECTED_LOWER_PRIORITY/STALE)
    and, when applied/superseding, the live row advances in place with its
    ``result`` FK repointed at the new event.

    Returns a :class:`WriteOutcome`; ``result_id`` always carries the appended
    event's PK. The applied/live_id/archived_history fields keep their meaning.
    """
    priority = SOURCE_PRIORITY[source_type]
    retrieved_at = retrieved_at or timezone.now()

    # Defense-in-depth: a mismatched key is STORED as an event but never updates
    # live. We record it under the requested key with the returned key captured.
    if norm.inchikey != inchikey:
        event = _append_event(
            requested_inchikey=inchikey,
            returned_inchikey=norm.inchikey,
            kind=ResultKind.REJECTED_MISMATCH,
            source_type=source_type,
            priority=priority,
            norm=norm,
            remote_query_id=remote_query_id,
            source_ref=source_ref,
            classification_version=norm.classification_version,
            retrieved_at=retrieved_at,
            source_artifact=source_artifact,
        )
        live_id = (
            ClassyfireLive.objects.filter(inchikey=inchikey)
            .values_list("id", flat=True)
            .first()
        )
        return WriteOutcome(
            applied=False,
            live_id=int(live_id) if live_id is not None else None,
            archived_history=False,
            reason="result key mismatch",
            result_id=event.id,
        )

    existing = (
        ClassyfireLive.objects.select_for_update()
        .filter(inchikey=inchikey)
        .first()
    )

    def _append(kind: ResultKind) -> ClassificationResult:
        return _append_event(
            requested_inchikey=inchikey,
            returned_inchikey=norm.inchikey,
            kind=kind,
            source_type=source_type,
            priority=priority,
            norm=norm,
            remote_query_id=remote_query_id,
            source_ref=source_ref,
            classification_version=norm.classification_version,
            retrieved_at=retrieved_at,
            source_artifact=source_artifact,
        )

    if existing is None:
        # No row yet — try to claim the unique slot with a create guarded by a
        # savepoint. We append the APPLIED event INSIDE the savepoint so a lost
        # create race rolls back the (now-misleading) event too; the winning
        # path re-decides precedence against the locked row and appends exactly
        # one decisive event. This keeps one event per logical write.
        try:
            with transaction.atomic():
                event = _append(ResultKind.APPLIED)
                row = ClassyfireLive(inchikey=inchikey)
                _apply_event_to_live(row, event)
                row.save()
            return WriteOutcome(
                applied=True,
                live_id=row.id,
                archived_history=False,
                result_id=event.id,
            )
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
        # a lower-priority result). Record the rejected event.
        event = _append(ResultKind.REJECTED_LOWER_PRIORITY)
        return WriteOutcome(
            applied=False,
            live_id=existing.id,
            archived_history=False,
            reason="existing higher priority",
            result_id=event.id,
        )

    if existing.source_priority == priority and existing.retrieved_at >= retrieved_at:
        event = _append(ResultKind.REJECTED_STALE)
        return WriteOutcome(
            applied=False,
            live_id=existing.id,
            archived_history=False,
            reason="existing same priority is newer or equal",
            result_id=event.id,
        )

    # >= priority: the new result supersedes. Append a SUPERSEDED event, snapshot
    # the prior row to history FIRST, then update the live row in place and
    # repoint its result FK (no delete+create window).
    event = _append(ResultKind.SUPERSEDED)
    _snapshot(existing).save()
    _apply_event_to_live(existing, event)
    existing.save(update_fields=_LIVE_FIELDS)
    return WriteOutcome(
        applied=True,
        live_id=existing.id,
        archived_history=True,
        result_id=event.id,
    )
