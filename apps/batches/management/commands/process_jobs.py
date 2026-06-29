"""``manage.py process_jobs`` — durable ingest-worker loop.

Claims batch jobs that still need local processing via
``SELECT ... FOR UPDATE SKIP LOCKED`` (mirroring the remote queue protocol),
advances the fine-grained ``processing_state`` machine, and runs the chunked,
checkpointed, bulk-lookup pipeline. Restart-safe: a job whose worker died is
re-claimable and resumes from PENDING_NORMALIZATION without double-processing.

Intended to run alongside ``runworker`` (which drains the REMOTE queue). This
command owns the LOCAL normalization/lookup/upsert/task-create work that the
HTTP upload routes defer.
"""
from __future__ import annotations

import logging
import socket
import time
import uuid
from typing import Any

from django.core.management.base import BaseCommand

from apps.batches.services import (
    IngestLeaseLost,
    claim_job_for_processing,
    process_claimed_job,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the durable batch-ingest processing loop (claim + process jobs)."

    # How often the loop runs the NEED_STRUCTURE auto-resolution sweep. The
    # resolver's per-provider rate limiter (not this interval) bounds outbound
    # call volume, so a short interval just keeps the queue draining.
    _RESOLVE_INTERVAL_SECONDS = 15.0

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--once",
            action="store_true",
            help="Drain the queue once and exit (for diagnostics/tests).",
        )
        parser.add_argument(
            "--idle-sleep",
            type=float,
            default=2.0,
            help="Seconds to sleep when no job needs processing.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        worker_id = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        once: bool = options["once"]
        idle_sleep: float = options["idle_sleep"]
        self.stdout.write(
            self.style.SUCCESS(f"process_jobs starting (worker_id={worker_id})")
        )
        # Seed in the past so the first cycle runs a sweep immediately.
        last_resolve = time.monotonic() - self._RESOLVE_INTERVAL_SECONDS
        try:
            while True:
                now = time.monotonic()
                if now - last_resolve >= self._RESOLVE_INTERVAL_SECONDS:
                    last_resolve = now
                    self._resolve_pending_structures()
                job = claim_job_for_processing(worker_id)
                if job is None:
                    if once:
                        break
                    time.sleep(idle_sleep)
                    continue
                try:
                    process_claimed_job(job)
                except IngestLeaseLost as exc:
                    # Another worker reclaimed this job mid-run (our lease went
                    # stale). The new owner now drives it; drop our stale handle
                    # and claim the next job rather than crashing the worker.
                    self.stdout.write(
                        self.style.WARNING(
                            f"lease lost for job {exc.job_pk}; "
                            "another worker took over"
                        )
                    )
        except KeyboardInterrupt:  # pragma: no cover - operator Ctrl-C
            self.stdout.write(self.style.WARNING("process_jobs stopping"))

    def _resolve_pending_structures(self) -> None:
        """Run one structure-resolution sweep; never let it crash the loop."""
        from apps.batches.structure_resolution import resolve_pending_structures

        try:
            stats = resolve_pending_structures()
        except Exception:  # pragma: no cover - sweep must never kill the loop
            logger.exception("structure-resolution sweep failed")
            return
        if stats.get("items"):
            logger.info(
                "structure resolve: %s key(s) → %s item(s) re-queued",
                stats["resolved_keys"],
                stats["items"],
            )
