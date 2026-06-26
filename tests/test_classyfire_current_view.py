"""Tests for the unified ``classyfire_current`` view (migration 0005).

These exercise the live-over-seed precedence through the public lookup API so a
regression in the view SQL or the tier dispatcher is caught. They require a real
PostgreSQL test database (the view + UNION ALL are not exercisable on sqlite).
"""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.classifications.models import ClassyfireLive, ClassyfireSeed
from apps.classifications.services import lookup_current_classification
from apps.core.enums import SOURCE_PRIORITY, SourceType

_LIVE_IK = "AAAAAAAAAAAAAA-BBBBBBBBBB-N"
_SEED_IK = "CCCCCCCCCCCCCC-DDDDDDDDDD-N"
_MISS_IK = "EEEEEEEEEEEEEE-FFFFFFFFFF-N"


@pytest.mark.django_db(transaction=True)
def test_current_view_live_wins_over_seed():
    """When both a live and a seed row exist, the live row is returned."""
    ClassyfireSeed.objects.create(
        inchikey=_LIVE_IK,
        inchikey14=_LIVE_IK[:14],
        seed_source_id=1,
        seed_version="t-seed",
    )
    ClassyfireLive.objects.create(
        inchikey=_LIVE_IK,
        source_type=SourceType.WISHART_LIVE_QUERY.value,
        source_priority=SOURCE_PRIORITY[SourceType.WISHART_LIVE_QUERY],
        retrieved_at=timezone.now(),
    )

    result = lookup_current_classification(_LIVE_IK)

    assert result is not None
    # Live precedence: the live source type, not the seed's HISTORICAL_AGGREGATE.
    assert result.source_type == SourceType.WISHART_LIVE_QUERY.value


@pytest.mark.django_db(transaction=True)
def test_current_view_falls_back_to_seed():
    """With no live row, the seed row is served as HISTORICAL_AGGREGATE."""
    ClassyfireSeed.objects.create(
        inchikey=_SEED_IK,
        inchikey14=_SEED_IK[:14],
        seed_source_id=7,
        seed_version="t-seed-v",
    )

    result = lookup_current_classification(_SEED_IK)

    assert result is not None
    assert result.source_type == SourceType.HISTORICAL_AGGREGATE.value
    assert result.classification_version == "t-seed-v"
    assert result.source_ref == "7"


@pytest.mark.django_db(transaction=True)
def test_current_view_miss_returns_none():
    """An InChIKey present in neither table resolves to None."""
    assert lookup_current_classification(_MISS_IK) is None
