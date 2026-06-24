from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.classifications.official_import import (
    OfficialImportError,
    import_official_file,
)


class Command(BaseCommand):
    help = (
        "Provenance-preserving official import (LAB_OFFICIAL_IMPORT): record an "
        "immutable SourceArtifact with sha256 checksum, emit immutable "
        "ClassificationResult event(s), and advance the live pointer."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("path", help="Path to the official-classification JSON artifact.")
        parser.add_argument(
            "--uri", default=None, help="Optional origin URI recorded on the artifact."
        )

    def handle(self, *args, **options) -> None:
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"Artifact not found: {path}")
        try:
            result = import_official_file(path, uri=options.get("uri"))
        except OfficialImportError as exc:
            raise CommandError(str(exc)) from exc

        applied = sum(1 for o in result.outcomes if o.applied)
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported artifact {result.artifact_id} "
                f"(sha256={result.checksum[:12]}…, {result.byte_size} bytes): "
                f"{result.record_count} record(s), {applied} applied to live."
            )
        )
