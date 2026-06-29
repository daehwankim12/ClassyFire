"""The submit batch size is clamped to Wishart's JSON page size (10/page).

Wishart paginates a completed /queries JSON response at 10 compounds/page and the
worker poll fetches only one page, so a batch larger than the page size would
strand every entity beyond page 1. ``_effective_batch_size`` enforces the cap
regardless of the (env-tunable) ``REMOTE_BATCH_SIZE``.
"""
from __future__ import annotations

from apps.remote.worker import WISHART_QUERY_JSON_PAGE_SIZE, _effective_batch_size


def test_effective_batch_size_clamps_above_page_size(settings):
    settings.REMOTE_BATCH_SIZE = 25
    assert _effective_batch_size() == WISHART_QUERY_JSON_PAGE_SIZE == 10


def test_effective_batch_size_passes_through_small_values(settings):
    settings.REMOTE_BATCH_SIZE = 3
    assert _effective_batch_size() == 3


def test_effective_batch_size_floors_at_one(settings):
    settings.REMOTE_BATCH_SIZE = 0
    assert _effective_batch_size() == 1
