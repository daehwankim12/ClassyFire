"""Tests for the Classification Lookup Service (Task 5).

FR-010/FR-011, P-02: exact 27-char InChIKey lookup, live-over-seed precedence,
newest-wins within equal priority, no auto-apply for 14-char connectivity keys.
All tests run against the real DB — no mocks.
"""
from __future__ import annotations

import datetime as dt
import statistics
import time
from datetime import datetime

import pytest
from django.db import connection

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

IK_A = "RDHQFKQIGNGIED-MRVPVSSYSA-N"  # real-format 27-char key
IK_B = "WQZGKKKJIJFFOK-GASJEMHNSA-N"
IK_MISSING = "AAAABBBBCCCCDD-EEEEFFFFFF-N"


def _make_chemont(numeric_id: int, name: str, version: str = "2.1") -> object:
    from apps.chemont.models import ChemOntNode

    return ChemOntNode.objects.get_or_create(
        numeric_id=numeric_id,
        defaults={
            "chemont_id": f"CHEMONT:{numeric_id:07d}",
            "name": name,
            "ontology_version": version,
        },
    )[0]


def _make_seed(inchikey: str, version: str = "v1", kingdom_id: int | None = None) -> object:
    from apps.classifications.models import ClassyfireSeed

    return ClassyfireSeed.objects.create(
        inchikey=inchikey,
        inchikey14=inchikey[:14],
        seed_source_id=1,
        seed_version=version,
        kingdom_id=kingdom_id,
    )


def _make_live(
    inchikey: str,
    source_type: str = "WISHART_LIVE_QUERY",
    source_priority: int = 100,
    retrieved_at: datetime | None = None,
    classification_version: str | None = "cf-2.1",
    source_ref: str | None = "ref-1",
    kingdom_id: int | None = None,
) -> object:
    from apps.classifications.models import ClassyfireLive

    if retrieved_at is None:
        retrieved_at = datetime(2024, 6, 1, 12, 0, 0, tzinfo=dt.UTC)

    return ClassyfireLive.objects.create(
        inchikey=inchikey,
        source_type=source_type,
        source_priority=source_priority,
        retrieved_at=retrieved_at,
        classification_version=classification_version,
        source_ref=source_ref,
        kingdom_id=kingdom_id,
    )


# ---------------------------------------------------------------------------
# TEST: exact seed lookup
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_exact_seed_lookup_hit():
    node = _make_chemont(0, "Chemical entities")
    _make_seed(IK_A, version="v1", kingdom_id=node.numeric_id)

    from apps.classifications.services import LookupTier, lookup_current_classification

    result = lookup_current_classification(IK_A)
    assert result is not None
    assert result.inchikey == IK_A
    assert result.tier == LookupTier.SEED
    assert result.classification_version == "v1"
    assert result.kingdom is not None
    assert result.kingdom.name == "Chemical entities"


# ---------------------------------------------------------------------------
# TEST: live-over-seed precedence
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_live_over_seed_precedence():
    node = _make_chemont(1, "Organic compounds")
    _make_seed(IK_A, version="v1", kingdom_id=node.numeric_id)
    _make_live(IK_A, kingdom_id=node.numeric_id)

    from apps.classifications.services import LookupTier, lookup_current_classification

    result = lookup_current_classification(IK_A)
    assert result is not None
    assert result.tier == LookupTier.LIVE
    assert result.source_type == "WISHART_LIVE_QUERY"


# ---------------------------------------------------------------------------
# TEST: newest retrieved_at returned (ORDER BY source_priority DESC, retrieved_at DESC)
# The classyfire_live table has a unique constraint on inchikey, so there can
# only be one live row per inchikey.  We verify the selector correctly returns
# the row's retrieved_at by inserting a row with a known timestamp and checking
# the result matches.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_newest_retrieved_at_returned():
    ts = datetime(2025, 3, 15, 9, 30, 0, tzinfo=dt.UTC)
    _make_live(IK_A, retrieved_at=ts)

    from apps.classifications.services import lookup_current_classification

    result = lookup_current_classification(IK_A)
    assert result is not None
    # retrieved_at may be tz-aware or tz-naive depending on USE_TZ; compare value
    assert result.retrieved_at is not None
    rt = result.retrieved_at
    if rt.tzinfo is None:
        rt = rt.replace(tzinfo=dt.UTC)
    assert rt == ts


# ---------------------------------------------------------------------------
# TEST: P-02 — no auto-apply for 14-char connectivity key
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_no_auto_apply_14char_key():
    # Even if a seed row exists, a 14-char key must return None.
    _make_seed(IK_A)

    from apps.classifications.services import lookup_current_classification

    result = lookup_current_classification(IK_A[:14])  # "RDHQFKQIGNGIED"
    assert result is None


@pytest.mark.django_db(transaction=True)
def test_no_auto_apply_wrong_format():
    from apps.classifications.services import lookup_current_classification

    assert lookup_current_classification("") is None
    assert lookup_current_classification("not-an-inchikey") is None
    assert lookup_current_classification("rdhqfkqigngied-mrvpvssysa-n") is None  # lowercase


# ---------------------------------------------------------------------------
# TEST: no match returns None
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_no_match_returns_none():
    from apps.classifications.services import lookup_current_classification

    result = lookup_current_classification(IK_MISSING)
    assert result is None


# ---------------------------------------------------------------------------
# TEST: provenance fields populated
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_provenance_fields_populated():
    ts = datetime(2024, 11, 20, 8, 0, 0, tzinfo=dt.UTC)
    _make_live(
        IK_A,
        source_type="ARCHIVED_WISHART_JSON",
        source_priority=90,
        retrieved_at=ts,
        classification_version="cf-2.2",
        source_ref="zenodo-12345",
    )

    from apps.classifications.services import lookup_current_classification

    result = lookup_current_classification(IK_A)
    assert result is not None
    assert result.source_type == "ARCHIVED_WISHART_JSON"
    assert result.source_priority == 90
    assert result.classification_version == "cf-2.2"
    assert result.source_ref == "zenodo-12345"
    assert result.retrieved_at is not None


# ---------------------------------------------------------------------------
# TEST: p95 latency on fixture-scale dataset
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_p95_latency_under_200ms():
    """p95 of exact-key lookups on 300 seed rows must be < 200 ms."""
    # Insert 300 seed rows via raw SQL for speed.
    rows = []
    for i in range(300):
        # Generate valid 27-char InChIKeys: 14-char block + 10-char block + 1-char
        block1 = f"AAAAAAAAAA{i:04d}"[:14]
        block2 = f"BBBBBB{i:04d}"[:10]
        suffix = "N"
        ik = f"{block1}-{block2}-{suffix}"
        rows.append((ik, ik[:14], i + 100, "perf-v1"))

    with connection.cursor() as cur:
        cur.executemany(
            "INSERT INTO classyfire_seed (inchikey, inchikey14, seed_source_id, seed_version)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            rows,
        )

    from apps.classifications.services import lookup_current_classification

    # 100 lookups: mix of hits (first 50) and misses
    hit_keys = [r[0] for r in rows[:50]]
    miss_keys = [IK_MISSING] * 50
    keys = hit_keys + miss_keys

    latencies: list[float] = []
    for key in keys:
        t0 = time.perf_counter()
        lookup_current_classification(key)
        latencies.append((time.perf_counter() - t0) * 1000)

    p95 = statistics.quantiles(latencies, n=100)[94]
    assert p95 < 200, f"p95 latency {p95:.1f} ms exceeded 200 ms threshold"
