"""Progress + rough-ETA computation for the batch job detail page.

The detail page shows a segmented progress bar (resolved vs still-processing) and
a coarse "time remaining" estimate. The ETA is intentionally approximate: most of
the remaining time is spent in rate-limited remote (Wishart) classification whose
throughput depends on cross-job queue contention, so we derive it from the
observed resolution rate since the job started and label it as an estimate. It
self-corrects on every poll.
"""
from __future__ import annotations

from typing import Any

from django.utils import timezone

from apps.batches.models import BatchJob


def _humanize_eta(seconds: float) -> str:
    total = int(seconds)
    if total < 60:
        return f"약 {max(1, total)}초 남음 (예상)"
    minutes = total // 60
    if minutes < 60:
        return f"약 {minutes}분 남음 (예상)"
    hours, rem_min = divmod(minutes, 60)
    return f"약 {hours}시간 {rem_min}분 남음 (예상)"


def build_job_progress(job: BatchJob, *, is_terminal: bool) -> dict[str, Any]:
    """Return the template context for the job progress bar.

    Segments: completed (classified), need-structure (terminal, awaits user),
    invalid (terminal), and pending (still processing). ``resolved`` is the sum
    of the three terminal segments; ``pending`` is whatever is left of the total.

    ``is_terminal`` is the caller's authoritative "the job will not change
    anymore" signal (``_is_terminal_job``). It — not a zero pending count —
    drives the poll-stop flag and the "완료" ETA, so a freshly created job whose
    items have not been counted yet (total 0, status PENDING) keeps polling.
    """
    total = job.total_count or 0
    completed = job.completed_count or 0
    need_structure = job.need_structure_count or 0
    invalid = job.invalid_count or 0

    resolved = min(total, completed + need_structure + invalid)
    pending = max(0, total - resolved)

    def pct(value: int) -> float:
        return round(100.0 * value / total, 1) if total else 0.0

    is_complete = is_terminal
    if is_terminal:
        eta = "완료"
    else:
        eta = "처리 중…"
        if pending > 0 and resolved > 0 and job.created_at is not None:
            elapsed = (timezone.now() - job.created_at).total_seconds()
            if elapsed > 0:
                rate = resolved / elapsed  # resolved items per second
                if rate > 0:
                    eta = _humanize_eta(pending / rate)

    return {
        "total": total,
        "completed": completed,
        "need_structure": need_structure,
        "invalid": invalid,
        "pending": pending,
        "resolved": resolved,
        "completed_pct": pct(completed),
        "need_structure_pct": pct(need_structure),
        "invalid_pct": pct(invalid),
        "pending_pct": pct(pending),
        "resolved_pct": pct(resolved),
        "eta": eta,
        "is_complete": is_complete,
    }
