"""``manage.py runworker`` — drain the remote ClassyFire queue.

Runs the claim → process → transition → release loop, respecting
``REMOTE_MAX_CONCURRENCY`` worker slots. Intended to run as the compose
``worker`` service. Uses :class:`WishartDirectProvider` by default; pass
``--mock`` for a smoke run with no network.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.remote.worker import RemoteWorker

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the remote ClassyFire work queue worker loop."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process a single tick per slot and exit (for diagnostics).",
        )
        parser.add_argument(
            "--mock",
            action="store_true",
            help="Use the deterministic MockProvider (no network).",
        )
        parser.add_argument(
            "--idle-sleep",
            type=float,
            default=2.0,
            help="Seconds to sleep when the queue is idle.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        provider = self._build_provider(mock=options["mock"])
        concurrency = max(1, settings.REMOTE_MAX_CONCURRENCY)
        workers = [RemoteWorker(provider) for _ in range(concurrency)]
        self.stdout.write(
            self.style.SUCCESS(
                f"runworker starting (concurrency={concurrency}, "
                f"remote_enabled={settings.REMOTE_ENABLED})"
            )
        )
        once = options["once"]
        idle_sleep = options["idle_sleep"]
        try:
            while True:
                # Tick EVERY worker each cycle — do NOT short-circuit. The old
                # ``any(worker.tick() for ...)`` stopped at the first truthy tick,
                # so only one worker ever ran per cycle. Materialise all results
                # so every worker claims + processes a task this cycle (F-08).
                results = [worker.tick() for worker in workers]
                did_work = any(results)
                if once:
                    break
                if not did_work:
                    time.sleep(idle_sleep)
        except KeyboardInterrupt:  # pragma: no cover - operator Ctrl-C
            self.stdout.write(self.style.WARNING("runworker stopping"))

    def _build_provider(self, *, mock: bool) -> Any:
        if mock:
            from apps.remote.mock import MockProvider

            return MockProvider()
        from apps.remote.wishart import WishartDirectProvider

        return WishartDirectProvider()
