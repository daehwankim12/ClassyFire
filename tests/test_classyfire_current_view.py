"""Tests for the unified ``classyfire_current`` view (live-only).

These exercise the live read surface through the public lookup API so a
regression in the view SQL or the row converter is caught. They require a real
PostgreSQL test database (the view is not exercisable on sqlite).
"""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.classifications.models import ClassyfireLive
from apps.classifications.services import lookup_current_classification
from apps.core.enums import SOURCE_PRIORITY, SourceType

_LIVE_IK = "AAAAAAAAAAAAAA-BBBBBBBBBB-N"
_MISS_IK = "EEEEEEEEEEEEEE-FFFFFFFFFF-N"


@pytest.mark.django_db(transaction=True)
def test_current_view_returns_live():
    """A live row is resolved through the view by the public lookup API."""
    ClassyfireLive.objects.create(
        inchikey=_LIVE_IK,
        source_type=SourceType.WISHART_LIVE_QUERY.value,
        source_priority=SOURCE_PRIORITY[SourceType.WISHART_LIVE_QUERY],
        retrieved_at=timezone.now(),
    )

    result = lookup_current_classification(_LIVE_IK)

    assert result is not None
    assert result.source_type == SourceType.WISHART_LIVE_QUERY.value


@pytest.mark.django_db(transaction=True)
def test_current_view_miss_returns_none():
    """An InChIKey with no live row resolves to None."""
    assert lookup_current_classification(_MISS_IK) is None
