from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.classifications.classyfire_web_csv import (
    ClassyfireWebCsvImportError,
    import_classyfire_web_csv_file,
)


def _load_mapping(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandError(f"Could not read mapping JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CommandError("Mapping JSON must be an object of CompoundID to InChIKey.")
    mapping: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise CommandError("Mapping JSON keys and values must be strings.")
        mapping[key] = value
    return mapping


class Command(BaseCommand):
    help = (
        "Import a Wishart ClassyFire web CSV export as WISHART_LIVE_QUERY results. "
        "The mapping JSON must map each web CompoundID to the local full InChIKey."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("path", help="Path to the ClassyFire web CSV export.")
        parser.add_argument(
            "--mapping",
            required=True,
            help="JSON file mapping web CompoundID values to full InChIKeys.",
        )
        parser.add_argument(
            "--uri",
            default=None,
            help="Optional origin URI recorded on the source artifact.",
        )

    def handle(self, *args, **options) -> None:
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"CSV artifact not found: {path}")
        mapping_path = Path(options["mapping"])
        if not mapping_path.exists():
            raise CommandError(f"Mapping file not found: {mapping_path}")
        mapping = _load_mapping(mapping_path)
        try:
            result = import_classyfire_web_csv_file(
                path,
                identifier_to_inchikey=mapping,
                uri=options.get("uri"),
            )
        except ClassyfireWebCsvImportError as exc:
            raise CommandError(str(exc)) from exc

        applied = sum(1 for outcome in result.outcomes if outcome.applied)
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported ClassyFire web CSV artifact {result.artifact_id} "
                f"(sha256={result.checksum[:12]}..., {result.byte_size} bytes): "
                f"{result.record_count} record(s), {applied} applied to live."
            )
        )
