"""``manage.py resolve_structures`` — one structure-resolution sweep (manual lever).

The ``process_jobs`` loop already runs this sweep periodically; this command lets
an operator run a pass on demand (optionally bounded with ``--limit``).
"""
from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.batches.structure_resolution import resolve_pending_structures


class Command(BaseCommand):
    help = "Resolve structures for NEED_STRUCTURE items from public DBs (one sweep)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Max distinct InChIKeys to attempt this pass (default: settings).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        stats = resolve_pending_structures(limit=options["limit"])
        self.stdout.write(
            self.style.SUCCESS(
                f"structure resolve: attempted {stats['keys']} key(s), "
                f"resolved {stats['resolved_keys']} key(s) / {stats['items']} item(s)"
            )
        )
