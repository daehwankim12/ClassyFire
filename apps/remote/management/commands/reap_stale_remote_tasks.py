"""``manage.py reap_stale_remote_tasks`` — terminally time out stuck tasks.

The remote queue never discards a task on its own: an exhausted task parks as
DEFERRED and retries ~daily forever, so a permanently-unresolvable InChIKey keeps
its BatchItem in REMOTE_DELAYED and pins the owning job to IN_PROGRESS. The
``runworker`` loop already runs this reaper once a minute; this command is the
manual lever an operator uses to clear a stuck run NOW (optionally with a shorter
horizon than ``REMOTE_DELAYED_TIMEOUT_HOURS``).
"""
from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.remote import queue


class Command(BaseCommand):
    help = "Time out remote tasks outstanding longer than --hours (DONE + REMOTE_TIMED_OUT)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--hours",
            type=float,
            default=None,
            help=(
                "Age threshold in hours (default: settings.REMOTE_DELAYED_TIMEOUT_HOURS). "
                "Tasks created more than this long ago are timed out."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        hours = options["hours"]
        if hours is None:
            hours = settings.REMOTE_DELAYED_TIMEOUT_HOURS
        timed_out = queue.timeout_stale_tasks(timeout_hours=hours)
        self.stdout.write(
            self.style.SUCCESS(
                f"timed out {timed_out} stale remote task(s) older than {hours}h"
            )
        )
