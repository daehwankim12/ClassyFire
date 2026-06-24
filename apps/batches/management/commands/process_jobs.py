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

import socket
import time
import uuid
from typing import Any

from django.core.management.base import BaseCommand

from apps.batches.services import claim_job_for_processing, process_claimed_job


class Command(BaseCommand):
    help = "Run the durable batch-ingest processing loop (claim + process jobs)."

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
        try:
            while True:
                job = claim_job_for_processing(worker_id)
                if job is None:
                    if once:
                        break
                    time.sleep(idle_sleep)
                    continue
                process_claimed_job(job)
        except KeyboardInterrupt:  # pragma: no cover - operator Ctrl-C
            self.stdout.write(self.style.WARNING("process_jobs stopping"))
