"""Management command: verify checksum of a local seed file against SeedImport."""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.classifications.seed_manifest import compute_sha256


class Command(BaseCommand):
    help = "Verify SHA-256 checksum of a local seed file."

    def add_arguments(self, parser):
        parser.add_argument("source", help="Path to the local .tsv.zst file.")
        parser.add_argument(
            "--expected",
            default=None,
            help="Expected SHA-256 hex digest.  If omitted, just print the digest.",
        )
        parser.add_argument(
            "--seed-import-id",
            type=int,
            default=None,
            help="SeedImport PK; reads expected checksum from that row.",
        )

    def handle(self, *args, **options):
        source = Path(options["source"])
        if not source.exists():
            raise CommandError(f"File not found: {source}")

        actual = compute_sha256(source)
        self.stdout.write(f"SHA-256: {actual}")

        expected = options["expected"]
        if not expected and options["seed_import_id"]:
            from apps.operations.models import SeedImport

            try:
                row = SeedImport.objects.get(pk=options["seed_import_id"])
            except SeedImport.DoesNotExist as exc:
                raise CommandError(
                    f"SeedImport {options['seed_import_id']} not found"
                ) from exc
            expected = row.checksum

        if expected:
            if actual == expected:
                self.stdout.write(self.style.SUCCESS("Checksum OK."))
            else:
                raise CommandError(
                    f"Checksum mismatch!\n  expected: {expected}\n  actual:   {actual}"
                )
