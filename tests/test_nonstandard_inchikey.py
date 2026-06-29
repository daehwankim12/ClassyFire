"""Standard vs non-standard InChIKey handling (complete-A).

A bare non-standard InChIKey can't be matched against the standard-keyed caches
(InChIKey is a one-way hash), but a SUPPLIED structure (manual or auto-resolved)
is authoritative: we adopt its recomputed STANDARD key when the connectivity
skeleton matches, so the item is classified under the standard key. A standard
stored key still requires an exact match.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.hashers import make_password

from apps.batches.models import BatchItem, BatchJob
from apps.batches.services import StructureKeyMismatchError, submit_structure_for_item
from apps.compounds.inchikey import is_standard_inchikey, same_inchikey_skeleton
from apps.core.enums import BatchItemStatus
from apps.remote import structure_providers
from apps.remote.structure_providers import resolve_structure

ETHANOL_STD = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
ETHANOL_NONSTD = "LFQSCWFLJHTTHZ-UHFFFAOYNA-N"  # same skeleton, non-standard flag 'N'
METHANOL_STD = "OKKJLVBELUTLKV-UHFFFAOYSA-N"


# --- pure helpers (no DB) -------------------------------------------------


def test_is_standard_inchikey_flag():
    assert is_standard_inchikey(ETHANOL_STD) is True
    assert is_standard_inchikey(ETHANOL_NONSTD) is False
    assert is_standard_inchikey("garbage") is True  # non-key → not special-cased


def test_skeleton_match():
    assert same_inchikey_skeleton(ETHANOL_STD, ETHANOL_NONSTD) is True
    assert same_inchikey_skeleton(ETHANOL_STD, METHANOL_STD) is False


# --- submit adopts the standard key for a non-standard item ---------------


def _need_structure_item(inchikey: str) -> BatchItem:
    job = BatchJob.objects.create(secret_hash=make_password("s"), remote_enabled=True)
    return BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=inchikey,
        inchikey=inchikey,
        status=BatchItemStatus.NEED_STRUCTURE,
    )


@pytest.mark.django_db(transaction=True)
def test_submit_adopts_standard_key_for_nonstandard_item():
    item = _need_structure_item(ETHANOL_NONSTD)

    result = submit_structure_for_item(item.id, "CCO")  # ethanol structure

    assert result.inchikey == ETHANOL_STD  # re-indexed under the standard key
    assert result.status != BatchItemStatus.NEED_STRUCTURE


@pytest.mark.django_db(transaction=True)
def test_submit_rejects_unrelated_structure_for_nonstandard_item():
    item = _need_structure_item(ETHANOL_NONSTD)

    with pytest.raises(StructureKeyMismatchError):
        submit_structure_for_item(item.id, "CO")  # methanol — different skeleton


@pytest.mark.django_db(transaction=True)
def test_submit_still_rejects_mismatch_for_standard_item():
    """A STANDARD stored key must still match exactly (no skeleton relaxation)."""
    item = _need_structure_item(METHANOL_STD)

    with pytest.raises(StructureKeyMismatchError):
        submit_structure_for_item(item.id, "CCO")  # ethanol ≠ methanol


# --- resolver accepts a skeleton match for a non-standard requested key ----


@pytest.mark.django_db(transaction=True)
def test_resolver_accepts_skeleton_match_for_nonstandard_key(settings, monkeypatch):
    from django.core.cache import cache

    cache.clear()
    settings.STRUCTURE_RESOLVE_ENABLED = True
    settings.STRUCTURE_RESOLVE_PROVIDERS = ("pubchem", "cactus")

    def responder(method, url, *, json_body=None):
        if "cactus" in url:
            return (200, "CCO\n")  # ethanol structure
        return (404, "")  # pubchem miss (standard-keyed only)

    monkeypatch.setattr(structure_providers, "_request", responder)

    rs = resolve_structure(ETHANOL_NONSTD)
    assert rs is not None
    assert rs.source == "cactus"
    assert rs.structure == "CCO"
    cache.clear()
