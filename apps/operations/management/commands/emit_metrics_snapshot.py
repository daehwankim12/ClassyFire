from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from apps.operations.metrics import collect_metrics_snapshot


class Command(BaseCommand):
    help = "Emit a Gate 3 operations metrics snapshot."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit machine-readable JSON (the default output shape).",
        )
        parser.add_argument(
            "--data-path",
            default=None,
            help="Path used for disk usage metrics; defaults to BACKUP_DATA_PATH.",
        )

    def handle(self, *args, **options) -> None:
        payload = collect_metrics_snapshot(
            data_path=options.get("data_path")
        ).to_dict()
        self.stdout.write(json.dumps(payload, sort_keys=True))
