"""Tests for the job progress-bar context builder (apps.batches.progress)."""
from __future__ import annotations

import pytest

from apps.batches.models import BatchJob
from apps.batches.progress import build_job_progress


@pytest.mark.django_db
def test_progress_complete_job_has_no_pending_and_done_eta():
    job = BatchJob.objects.create(
        secret_hash="x",
        total_count=10,
        completed_count=8,
        need_structure_count=1,
        invalid_count=1,
    )
    p = build_job_progress(job)
    assert p["resolved"] == 10
    assert p["pending"] == 0
    assert p["resolved_pct"] == 100.0
    assert p["is_complete"] is True
    assert p["eta"] == "완료"


@pytest.mark.django_db
def test_progress_in_flight_reports_pending_and_an_estimate():
    job = BatchJob.objects.create(secret_hash="x", total_count=10, completed_count=4)
    p = build_job_progress(job)
    assert p["pending"] == 6
    assert p["resolved_pct"] == 40.0
    assert p["is_complete"] is False
    # Some resolved + nonzero elapsed → a rough, labelled estimate (not the
    # "처리 중…" fallback).
    assert p["eta"].endswith("(예상)")


@pytest.mark.django_db
def test_progress_nothing_resolved_yet_falls_back_to_processing():
    job = BatchJob.objects.create(secret_hash="x", total_count=5, completed_count=0)
    p = build_job_progress(job)
    assert p["pending"] == 5
    assert p["is_complete"] is False
    assert p["eta"] == "처리 중…"


@pytest.mark.django_db
def test_progress_zero_total_is_safe():
    job = BatchJob.objects.create(secret_hash="x", total_count=0)
    p = build_job_progress(job)
    assert p["total"] == 0
    assert p["resolved_pct"] == 0.0
    assert p["pending"] == 0
    assert p["is_complete"] is True
