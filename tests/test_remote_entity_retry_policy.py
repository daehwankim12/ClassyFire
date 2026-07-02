"""Policy tests for entity retry escalation."""
from __future__ import annotations

from apps.remote.worker import _should_escalate_entity_retry_to_submit


def test_entity_5xx_with_structure_escalates_without_waiting_for_backoff():
    assert _should_escalate_entity_retry_to_submit(
        has_structure=True,
        retry_after=None,
        http_status=500,
    )


def test_entity_5xx_with_retry_after_does_not_escalate():
    assert not _should_escalate_entity_retry_to_submit(
        has_structure=True,
        retry_after=600.0,
        http_status=500,
    )


def test_entity_5xx_without_structure_does_not_escalate():
    assert not _should_escalate_entity_retry_to_submit(
        has_structure=False,
        retry_after=None,
        http_status=500,
    )


def test_entity_rate_limit_does_not_escalate():
    assert not _should_escalate_entity_retry_to_submit(
        has_structure=True,
        retry_after=None,
        http_status=429,
    )
