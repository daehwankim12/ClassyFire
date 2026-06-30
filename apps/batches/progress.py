"""Progress + rough-ETA computation for the batch job detail page.

The detail page shows a segmented progress bar (resolved vs still-processing) and
a coarse "time remaining" estimate. The ETA is intentionally approximate but is
floored at the **remote pace**: anything still pending has already missed the
local cache and must be classified by the rate-limited Wishart remote, which
costs ``60 / REMOTE_ENTITY_REQUESTS_PER_MIN`` seconds per item (see
:func:`_remote_pace_seconds`). The observed
resolution-rate estimate alone is misleading because it averages in the *instant*
local cache hits, so it badly under-promises the remote-bound tail. We
therefore take whichever of {remote-pace floor, observed rate} is slower and
label it an estimate. It self-corrects on every poll.
"""
from __future__ import annotations

from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.batches.models import BatchJob


def _remote_pace_seconds() -> float:
    """Seconds per remaining item, derived from the Wishart remote rate limit.

    Every still-pending item has missed the local cache, so it needs at
    least one rate-limited remote round-trip: an entity GET, then (for a genuine
    miss) a batched structure submit + polls. All remote calls draw from shared,
    DB-backed token buckets that refill at ``REMOTE_ENTITY_REQUESTS_PER_MIN`` /
    ``REMOTE_SUBMITS_PER_MIN`` per minute, so global remote throughput is capped
    at roughly that many items per minute **no matter how many workers run**.

    The binding per-item constraint is the entity bucket: there is exactly one
    entity GET per item, whereas submits batch up to the page size and are
    comparatively cheap. So we pace at ``60 / entity-rate`` seconds per item
    (e.g. 0.5s at 120/min). This is a floor — polls share the same entity bucket
    and cross-job contention only slows it further — but it is far closer than a
    rate that counts instant local hits.
    """
    rate = getattr(settings, "REMOTE_ENTITY_REQUESTS_PER_MIN", 120) or 120
    return 60.0 / max(int(rate), 1)


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
    elif pending <= 0:
        # Not terminal but nothing left to resolve (e.g. counts not computed
        # yet): keep polling without a misleading number.
        eta = "처리 중…"
    else:
        # Floor at the remote pace: each pending item costs ~6s of rate-limited
        # Wishart time. Only let the observed rate win when it is SLOWER (heavy
        # cross-job contention) — never faster, since its speed comes from
        # instant local cache hits that the remote tail won't enjoy.
        eta_seconds = pending * _remote_pace_seconds()
        if resolved > 0 and job.created_at is not None:
            elapsed = (timezone.now() - job.created_at).total_seconds()
            if elapsed > 0:
                rate = resolved / elapsed  # resolved items per second
                if rate > 0:
                    eta_seconds = max(eta_seconds, pending / rate)
        eta = _humanize_eta(eta_seconds)

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
