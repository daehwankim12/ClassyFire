"""Tests for NEED_STRUCTURE auto-resolution from public key-free structure DBs.

The provider HTTP chokepoint (structure_providers._request) is patched; rate
buckets + circuit breakers + RDKit verification (normalize_row) run for real.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.hashers import make_password

from apps.batches.models import BatchItem, BatchJob
from apps.batches.structure_resolution import resolve_pending_structures
from apps.core.enums import BatchItemStatus
from apps.remote import structure_providers
from apps.remote.structure_providers import resolve_structure

pytestmark = pytest.mark.django_db(transaction=True)

ETHANOL_IK = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
ETHANOL_INCHI = "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3"
ETHANOL_SMILES = "CCO"
METHANOL_INCHI = "InChI=1S/CH4O/c1-2/h2H,1H3"


@pytest.fixture(autouse=True)
def _clear_resolve_cache():
    """Isolate the negative (miss) cache between tests."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def _enable(settings, providers=("pubchem", "cactus")) -> None:
    settings.STRUCTURE_RESOLVE_ENABLED = True
    settings.STRUCTURE_RESOLVE_PROVIDERS = tuple(providers)


def _pubchem_inchi_body(inchi: str) -> str:
    return json.dumps(
        {"PropertyTable": {"Properties": [{"CID": 702, "InChI": inchi}]}}
    )


def test_resolve_returns_verified_pubchem_inchi(settings, monkeypatch):
    _enable(settings)

    def responder(method, url, *, json_body=None):
        if "pubchem" in url:
            return (200, _pubchem_inchi_body(ETHANOL_INCHI))
        return (404, "")

    monkeypatch.setattr(structure_providers, "_request", responder)

    rs = resolve_structure(ETHANOL_IK)
    assert rs is not None
    assert rs.source == "pubchem"
    assert rs.structure == ETHANOL_INCHI


def test_resolve_falls_through_to_cactus_on_pubchem_miss(settings, monkeypatch):
    _enable(settings)

    def responder(method, url, *, json_body=None):
        if "pubchem" in url:
            return (404, "")  # clean miss
        if "cactus" in url:
            return (200, ETHANOL_SMILES + "\n")
        return (404, "")

    monkeypatch.setattr(structure_providers, "_request", responder)

    rs = resolve_structure(ETHANOL_IK)
    assert rs is not None
    assert rs.source == "cactus"
    assert rs.structure == ETHANOL_SMILES


def test_resolve_rejects_structure_that_rehashes_to_different_key(settings, monkeypatch):
    """A provider returning a structure for the WRONG compound is rejected by the
    InChIKey re-hash check; with no other hit, resolution yields None."""
    _enable(settings)

    def responder(method, url, *, json_body=None):
        if "pubchem" in url:
            return (200, _pubchem_inchi_body(METHANOL_INCHI))  # wrong compound
        return (404, "")  # cactus miss

    monkeypatch.setattr(structure_providers, "_request", responder)

    assert resolve_structure(ETHANOL_IK) is None


def test_resolve_disabled_makes_no_network_calls(settings, monkeypatch):
    settings.STRUCTURE_RESOLVE_ENABLED = False
    settings.STRUCTURE_RESOLVE_PROVIDERS = ("pubchem", "cactus")

    def boom(*args, **kwargs):
        raise AssertionError("resolver must not call out when disabled")

    monkeypatch.setattr(structure_providers, "_request", boom)

    assert resolve_structure(ETHANOL_IK) is None


def test_sweep_resolves_need_structure_item_and_records_provenance(settings, monkeypatch):
    _enable(settings)

    def responder(method, url, *, json_body=None):
        if "pubchem" in url:
            return (200, _pubchem_inchi_body(ETHANOL_INCHI))
        return (404, "")

    monkeypatch.setattr(structure_providers, "_request", responder)

    job = BatchJob.objects.create(secret_hash=make_password("s"), remote_enabled=True)
    item = BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=ETHANOL_IK,
        inchikey=ETHANOL_IK,
        status=BatchItemStatus.NEED_STRUCTURE,
    )

    stats = resolve_pending_structures()
    assert stats["resolved_keys"] == 1
    assert stats["items"] == 1

    item.refresh_from_db()
    # Re-queued for calculation — no longer awaiting a user-supplied structure.
    assert item.status != BatchItemStatus.NEED_STRUCTURE

    from apps.compounds.models import CompoundStructure

    comp = CompoundStructure.objects.get(inchikey=ETHANOL_IK)
    assert comp.parse_metadata.get("structure_source") == "pubchem"
    assert comp.parse_metadata.get("structure_auto_resolved") is True


def test_definitive_miss_is_negative_cached(settings, monkeypatch):
    """A compound that EVERY provider definitively misses is cached so the next
    sweep does not re-query the external services."""
    _enable(settings)
    calls = {"n": 0}

    def responder(method, url, *, json_body=None):
        calls["n"] += 1
        return (404, "")  # definitive miss from every provider

    monkeypatch.setattr(structure_providers, "_request", responder)

    assert resolve_structure(ETHANOL_IK) is None
    first = calls["n"]
    assert first >= 1  # providers were actually queried

    assert resolve_structure(ETHANOL_IK) is None
    assert calls["n"] == first  # second call served from negative cache, no HTTP


def test_sweep_noop_when_disabled(settings):
    settings.STRUCTURE_RESOLVE_ENABLED = False
    stats = resolve_pending_structures()
    assert stats == {"keys": 0, "resolved_keys": 0, "items": 0}
