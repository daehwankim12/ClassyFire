"""DbRateLimiter.from_settings pins both bursts to 1 for smooth pacing.

The entity and submit token buckets refill at their per-minute settings but their
burst (capacity) is pinned to 1, so the budget is spent evenly at the refill
cadence rather than as a thundering-herd spike after an idle period.
"""
from __future__ import annotations

import pytest

from apps.remote.rate_limit import DbRateLimiter


def test_from_settings_pins_burst_to_one(settings):
    settings.REMOTE_ENTITY_REQUESTS_PER_MIN = 120
    settings.REMOTE_SUBMITS_PER_MIN = 10

    lim = DbRateLimiter.from_settings()

    # No catch-up burst on either bucket.
    assert lim.entity.capacity == 1
    assert lim.submit.capacity == 1
    # Refill cadence still tracks the per-minute settings (120/min = 2/sec).
    assert lim.entity.refill_rate_per_sec == pytest.approx(120 / 60)
    assert lim.submit.refill_rate_per_sec == pytest.approx(10 / 60)
