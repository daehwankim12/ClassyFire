"""Management command: import ChemOnt ontology from a TSV file."""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.chemont.importers import ChemOntImportError, import_chemont


class Command(BaseCommand):
    help = "Import ChemOnt ontology nodes from a TSV file."

    def add_arguments(self, parser):
        parser.add_argument(
            "tsv_path",
            help="Path to chemont_dictionary.tsv",
        )
        parser.add_argument(
            "--ontology-version",
            default=None,
            help="Override the ontology_version column in the file.",
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            default=False,
            help="Delete all existing ChemOnt nodes before import.",
        )

    def handle(self, *args, **options):
        tsv_path = options["tsv_path"]
        try:
            result = import_chemont(
                tsv_path,
                ontology_version=options["ontology_version"],
                replace=options["replace"],
            )
        except (FileNotFoundError, ChemOntImportError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"ChemOnt import complete: {result}"
            )
        )
