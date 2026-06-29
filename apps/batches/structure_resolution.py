"""Background sweep: auto-resolve structures for NEED_STRUCTURE items.

For each distinct InChIKey still stuck in NEED_STRUCTURE, ask the public,
key-free structure resolvers (apps.remote.structure_providers) for a structure
and — on a VERIFIED hit (it re-hashes to the same InChIKey) — feed it through the
normal user-structure path (submit_structure_for_item), which re-checks the
InChIKey and re-enqueues the item for Wishart calculation.

Strictly additive: an unresolved key stays NEED_STRUCTURE exactly as before. The
resolver chain is rate-limited per provider, so this sweep is cheap to run often;
it drains steadily without hammering any external service.
"""
from __future__ import annotations

import logging

from django.conf import settings

from apps.batches.models import BatchItem
from apps.batches.services import (
    StructureKeyMismatchError,
    StructureSubmissionConflictError,
    StructureSubmissionError,
    submit_structure_for_item,
)
from apps.core.enums import BatchItemStatus
from apps.remote.structure_providers import resolve_structure

logger = logging.getLogger(__name__)


def resolve_pending_structures(limit: int | None = None) -> dict[str, int]:
    """Run one structure-resolution sweep pass.

    Returns ``{"keys": distinct keys attempted, "resolved_keys": keys resolved,
    "items": items transitioned out of NEED_STRUCTURE}``.
    """
    if not getattr(settings, "STRUCTURE_RESOLVE_ENABLED", False):
        return {"keys": 0, "resolved_keys": 0, "items": 0}
    if limit is None:
        limit = int(getattr(settings, "STRUCTURE_RESOLVE_SWEEP_LIMIT", 25))

    inchikeys = list(
        BatchItem.objects.filter(status=BatchItemStatus.NEED_STRUCTURE)
        .exclude(inchikey__isnull=True)
        .exclude(inchikey="")
        .values_list("inchikey", flat=True)
        .distinct()[:limit]
    )

    resolved_keys = 0
    resolved_items = 0
    for inchikey in inchikeys:
        resolved = resolve_structure(inchikey)
        if resolved is None:
            continue
        # Apply to EVERY item (across jobs) still waiting on this key. Each
        # submit_structure_for_item is independently row-locked + re-verifies.
        item_ids = list(
            BatchItem.objects.filter(
                status=BatchItemStatus.NEED_STRUCTURE, inchikey=inchikey
            ).values_list("id", flat=True)
        )
        applied = False
        resolved_inchikeys: set[str] = set()
        for item_id in item_ids:
            try:
                updated_item = submit_structure_for_item(item_id, resolved.structure)
            except StructureSubmissionConflictError:
                continue  # raced to another state — leave it
            except (StructureKeyMismatchError, StructureSubmissionError):
                # resolve_structure already verified the key, so this is
                # unexpected; skip defensively rather than mark anything wrong.
                logger.warning(
                    "auto-resolved structure for %s rejected by submit (item %s)",
                    inchikey,
                    item_id,
                )
                continue
            applied = True
            if updated_item.inchikey:
                resolved_inchikeys.add(updated_item.inchikey)
            resolved_items += 1
        if applied:
            resolved_keys += 1
            for resolved_inchikey in resolved_inchikeys or {inchikey}:
                _stamp_provenance(resolved_inchikey, resolved.source)

    return {"keys": len(inchikeys), "resolved_keys": resolved_keys, "items": resolved_items}


def _stamp_provenance(inchikey: str, source: str) -> None:
    """Record on the resolved compound that its structure was auto-fetched.

    Provenance for the audit trail: the eventual classification is computed from
    a structure WE fetched, not one the user supplied.
    """
    from apps.compounds.models import CompoundStructure

    comp = CompoundStructure.objects.filter(inchikey=inchikey).first()
    if comp is None:
        return
    meta = dict(comp.parse_metadata or {})
    if meta.get("structure_source") == source and meta.get("structure_auto_resolved"):
        return
    meta["structure_source"] = source
    meta["structure_auto_resolved"] = True
    comp.parse_metadata = meta
    comp.save(update_fields=["parse_metadata"])
