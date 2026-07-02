"""Policy tests for entity retry escalation."""
from __future__ import annotations

from apps.remote.worker import (
    _should_escalate_entity_retry_to_submit,
    _should_terminalize_entity_retry_as_need_structure,
)


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


def test_repeated_entity_5xx_with_retry_after_escalates_on_fifth_failure():
    assert _should_escalate_entity_retry_to_submit(
        has_structure=True,
        retry_after=300.0,
        http_status=500,
        attempt=4,
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


def test_entity_5xx_without_structure_goes_to_need_structure():
    assert _should_terminalize_entity_retry_as_need_structure(
        has_structure=False,
        retry_after=None,
        http_status=500,
    )


def test_entity_5xx_with_structure_does_not_go_to_need_structure():
    assert not _should_terminalize_entity_retry_as_need_structure(
        has_structure=True,
        retry_after=None,
        http_status=500,
    )


def test_entity_5xx_with_retry_after_does_not_go_to_need_structure():
    assert not _should_terminalize_entity_retry_as_need_structure(
        has_structure=False,
        retry_after=600.0,
        http_status=500,
    )


def test_repeated_entity_5xx_with_retry_after_goes_to_need_structure_on_fifth_failure():
    assert _should_terminalize_entity_retry_as_need_structure(
        has_structure=False,
        retry_after=300.0,
        http_status=500,
        attempt=4,
    )


def test_entity_rate_limit_does_not_go_to_need_structure():
    assert not _should_terminalize_entity_retry_as_need_structure(
        has_structure=False,
        retry_after=None,
        http_status=429,
    )
