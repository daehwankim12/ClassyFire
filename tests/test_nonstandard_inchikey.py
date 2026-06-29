"""Standard vs non-standard InChIKey handling.

A bare non-standard InChIKey can't be matched against the standard-keyed caches
(InChIKey is a one-way hash), but a SUPPLIED structure (manual or auto-resolved)
is authoritative: we adopt its recomputed STANDARD key when the two keys match
on connectivity AND the block-2 stereo/isotope/charge hash — i.e. differ ONLY by
the standard/non-standard flag. A bare connectivity-skeleton match is NOT enough
(stereoisomers share connectivity), and a standard stored key still requires an
exact match.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.hashers import make_password

from apps.batches.models import BatchItem, BatchJob
from apps.batches.services import StructureKeyMismatchError, submit_structure_for_item
from apps.compounds.inchikey import (
    is_standard_inchikey,
    same_inchikey_modulo_flag,
    same_inchikey_skeleton,
)
from apps.core.enums import BatchItemStatus
from apps.remote import structure_providers
from apps.remote.structure_providers import resolve_structure

ETHANOL_STD = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
ETHANOL_NONSTD = "LFQSCWFLJHTTHZ-UHFFFAOYNA-N"  # same skeleton, non-standard flag 'N'
# Same connectivity block as ethanol but a DIFFERENT block-2 hash — i.e. a
# distinct stereo/isotope variant that a skeleton-only check would wrongly accept.
ETHANOL_DIFF_STEREO = "LFQSCWFLJHTTHZ-ABCDEFGHNA-N"
# Differs from ETHANOL_STD by the flag (S→N) AND the protonation block (N→O).
ETHANOL_NONSTD_DIFF_PROTON = "LFQSCWFLJHTTHZ-UHFFFAOYNA-O"
# Differs from ETHANOL_STD by the flag (S→N) AND the version char (A→B).
ETHANOL_NONSTD_DIFF_VERSION = "LFQSCWFLJHTTHZ-UHFFFAOYNB-N"
METHANOL_STD = "OKKJLVBELUTLKV-UHFFFAOYSA-N"


# --- pure helpers (no DB) -------------------------------------------------


def test_is_standard_inchikey_flag():
    assert is_standard_inchikey(ETHANOL_STD) is True
    assert is_standard_inchikey(ETHANOL_NONSTD) is False
    assert is_standard_inchikey("garbage") is True  # non-key → not special-cased


def test_skeleton_match():
    assert same_inchikey_skeleton(ETHANOL_STD, ETHANOL_NONSTD) is True
    assert same_inchikey_skeleton(ETHANOL_STD, METHANOL_STD) is False


def test_modulo_flag_match():
    # Same molecule, differing ONLY by the standard/non-standard flag → match.
    assert same_inchikey_modulo_flag(ETHANOL_STD, ETHANOL_NONSTD) is True
    # Same connectivity but a DIFFERENT block-2 hash (a stereoisomer): the
    # skeleton check passes, but modulo-flag correctly REJECTS it.
    assert same_inchikey_skeleton(ETHANOL_STD, ETHANOL_DIFF_STEREO) is True
    assert same_inchikey_modulo_flag(ETHANOL_STD, ETHANOL_DIFF_STEREO) is False
    # Different connectivity → no match.
    assert same_inchikey_modulo_flag(ETHANOL_STD, METHANOL_STD) is False
    # ONLY the flag char (index 23) may differ. A key that also differs in the
    # protonation block (index 26) or the version char (index 24) is NOT a match,
    # even though its block-1 + block-2 hash are identical.
    assert same_inchikey_modulo_flag(ETHANOL_STD, ETHANOL_NONSTD_DIFF_PROTON) is False
    assert same_inchikey_modulo_flag(ETHANOL_STD, ETHANOL_NONSTD_DIFF_VERSION) is False
    # The flag char must be a valid S/N in both keys; an arbitrary char at
    # index 23 is a malformed key, not a std/non-std variant.
    assert same_inchikey_modulo_flag(ETHANOL_STD, "LFQSCWFLJHTTHZ-UHFFFAOYXA-N") is False
    # Non-key inputs → False.
    assert same_inchikey_modulo_flag("garbage", ETHANOL_STD) is False
    assert same_inchikey_modulo_flag(None, ETHANOL_STD) is False


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


# --- resolver accepts a modulo-flag match for a non-standard requested key --


@pytest.mark.django_db(transaction=True)
def test_resolver_accepts_modulo_flag_match_for_nonstandard_key(settings, monkeypatch):
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
