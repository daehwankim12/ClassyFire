"""DbRateLimiter.from_settings pins Wishart calls to one smooth request bucket.

Entity GETs, query polls, and query POSTs share one DB-backed request bucket
because Wishart 429s are IP-wide in practice. The burst is pinned to 1, so the
budget is spent evenly at the refill cadence rather than as a thundering-herd
spike after an idle period.
"""
from __future__ import annotations

import pytest

from apps.remote.rate_limit import DbRateLimiter


def test_from_settings_pins_burst_to_one(settings):
    settings.REMOTE_ENTITY_REQUESTS_PER_MIN = 120
    settings.REMOTE_SUBMITS_PER_MIN = 10

    lim = DbRateLimiter.from_settings()

    # No catch-up burst, and GET/POST share the same upstream request bucket.
    assert lim.entity is lim.submit
    assert lim.entity.capacity == 1
    assert lim.submit.capacity == 1
    # Refill cadence tracks the stricter whole-provider request setting.
    assert lim.entity.refill_rate_per_sec == pytest.approx(10 / 60)
    assert lim.submit.refill_rate_per_sec == pytest.approx(10 / 60)


def test_from_settings_uses_one_wishart_request_bucket(settings):
    settings.REMOTE_ENTITY_REQUESTS_PER_MIN = 10
    settings.REMOTE_SUBMITS_PER_MIN = 12

    lim = DbRateLimiter.from_settings()

    assert lim.entity is lim.submit
    assert lim.entity.bucket == "request"
    assert lim.entity.refill_rate_per_sec == pytest.approx(10 / 60)


def test_db_entity_retry_delay_matches_refill_cadence(db):
    lim = DbRateLimiter(entity_per_min=120, submit_per_min=0, entity_capacity=1)

    assert lim.entity.allow() is True
    assert lim.entity.allow() is False

    delay = lim.entity.retry_after_seconds()
    assert 0 < delay <= 0.55
