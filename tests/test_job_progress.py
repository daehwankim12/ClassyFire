"""Tests for the job progress-bar context builder (apps.batches.progress)."""
from __future__ import annotations

import pytest

from apps.batches.models import BatchJob
from apps.batches.progress import build_job_progress


@pytest.mark.django_db
def test_progress_terminal_job_is_complete():
    job = BatchJob.objects.create(
        secret_hash="x",
        total_count=10,
        completed_count=8,
        need_structure_count=1,
        invalid_count=1,
    )
    p = build_job_progress(job, is_terminal=True)
    assert p["resolved"] == 10
    assert p["pending"] == 0
    assert p["resolved_pct"] == 100.0
    assert p["is_complete"] is True
    assert p["eta"] == "완료"


@pytest.mark.django_db
def test_progress_in_flight_reports_pending_and_an_estimate():
    job = BatchJob.objects.create(secret_hash="x", total_count=10, completed_count=4)
    p = build_job_progress(job, is_terminal=False)
    assert p["pending"] == 6
    assert p["resolved_pct"] == 40.0
    assert p["is_complete"] is False
    assert p["eta"].endswith("(예상)")


@pytest.mark.django_db
def test_progress_nothing_resolved_yet_still_estimates_from_remote_pace():
    """Even before anything resolves, the pending items are remote-bound, so the
    ETA is floored at the ~6s/item Wishart pace (not 'processing…')."""
    job = BatchJob.objects.create(secret_hash="x", total_count=5, completed_count=0)
    p = build_job_progress(job, is_terminal=False)
    assert p["pending"] == 5
    assert p["is_complete"] is False
    assert p["eta"].endswith("(예상)")


@pytest.mark.django_db
def test_progress_eta_floored_at_remote_pace_not_cache_speed(settings):
    """The observed resolution rate is fast (instant cache/seed hits) but the 10
    pending items are remote-bound: the ETA must reflect the remote pace, not the
    near-instant cache speed. Pin the entity rate so the assertion is independent
    of the (tunable) default."""
    settings.REMOTE_ENTITY_REQUESTS_PER_MIN = 10
    job = BatchJob.objects.create(
        secret_hash="x", total_count=20, completed_count=10
    )
    p = build_job_progress(job, is_terminal=False)
    assert p["pending"] == 10
    # 10 items x 60/REMOTE_ENTITY_REQUESTS_PER_MIN(=10) s = 60s -> "약 1분 남음".
    assert p["eta"] == "약 1분 남음 (예상)"


@pytest.mark.django_db
def test_progress_pending_job_without_counts_keeps_polling():
    """A freshly created job (items not counted yet) is NOT terminal, so the bar
    must keep polling even though pending == 0."""
    job = BatchJob.objects.create(secret_hash="x", total_count=0)
    p = build_job_progress(job, is_terminal=False)
    assert p["total"] == 0
    assert p["resolved_pct"] == 0.0
    assert p["is_complete"] is False
    assert p["eta"] == "처리 중…"
