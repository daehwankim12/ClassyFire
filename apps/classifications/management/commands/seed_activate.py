"""Management command: atomically swap classyfire_seed_current view.

ONLY activates if audit passed (SeedImport.import_status == 'AUDIT_PASS').

The view swap is performed inside a single transaction using
``CREATE OR REPLACE VIEW``, which is atomic in PostgreSQL — no read window is
lost between the old and new view definitions.

After activation:
  - ``SeedImport.import_status`` is set to ``ACTIVE``.
  - ``SeedImport.activated_table`` records the physical table name.
  - A ``SystemEvent`` of category ``SEED`` is emitted.

Any-language reader must query ``classyfire_seed_current``; app code never
hard-codes the physical versioned table name.
"""
from __future__ import annotations

import psycopg.sql as pgsql
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.classifications.seed_manifest import CURRENT_VIEW_NAME


class Command(BaseCommand):
    help = (
        "Atomically swap classyfire_seed_current to point at the validated seed table."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--seed-import-id",
            type=int,
            default=None,
            help="SeedImport PK to activate.  Defaults to the latest AUDIT_PASS row.",
        )
        parser.add_argument(
            "--seed-version-label",
            default=None,
            dest="version",
            help="Logical version label (e.g. 'fixture', '2024-v1').  Informational only.",
        )
        parser.add_argument(
            "--table",
            default=None,
            help="Physical table to point the view at.  Defaults to classyfire_seed.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            default=False,
            help="Activate even if audit status is not AUDIT_PASS (use with care).",
        )

    def handle(self, *args, **options):
        from apps.core.enums import EventCategory
        from apps.operations.models import SeedImport, SystemEvent

        seed_import_id = options["seed_import_id"]
        force = options["force"]

        seed_import: SeedImport
        if seed_import_id:
            try:
                seed_import = SeedImport.objects.get(pk=seed_import_id)
            except SeedImport.DoesNotExist as exc:
                raise CommandError(f"SeedImport {seed_import_id} not found") from exc
        else:
            candidate = (
                SeedImport.objects.filter(import_status="AUDIT_PASS")
                .order_by("-created_at")
                .first()
            )
            if not candidate:
                raise CommandError(
                    "No AUDIT_PASS SeedImport rows found.  Run seed_audit first."
                )
            seed_import = candidate

        if seed_import.import_status != "AUDIT_PASS" and not force:
            raise CommandError(
                f"SeedImport pk={seed_import.pk} has status "
                f"'{seed_import.import_status}', not AUDIT_PASS.  "
                f"Run seed_audit or use --force."
            )

        physical_table = (
            options["table"]
            or seed_import.activated_table
            or "classyfire_seed"
        )
        version_label = options["version"] or str(seed_import.pk)

        self.stdout.write(
            f"Activating {physical_table} as {CURRENT_VIEW_NAME} "
            f"(SeedImport pk={seed_import.pk}, version={version_label})..."
        )

        with transaction.atomic():
            # Ensure the physical table exists.
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = %s AND table_schema = 'public')",
                    [physical_table],
                )
                exists: bool = cur.fetchone()[0]
                if not exists:
                    raise CommandError(
                        f"Physical table '{physical_table}' does not exist in DB."
                    )

            # Atomically create or replace the view.
            with connection.cursor() as cur:
                cur.execute(
                    pgsql.SQL("CREATE OR REPLACE VIEW {} AS SELECT * FROM {}").format(
                        pgsql.Identifier(CURRENT_VIEW_NAME),
                        pgsql.Identifier(physical_table),
                    )
                )

            # Update SeedImport row.
            seed_import.activated_table = physical_table
            seed_import.import_status = "ACTIVE"
            seed_import.save(update_fields=["activated_table", "import_status"])

            # Emit SystemEvent.
            SystemEvent.objects.create(
                category=EventCategory.SEED,
                event_type="seed_activate",
                payload={
                    "seed_import_id": seed_import.pk,
                    "physical_table": physical_table,
                    "version": version_label,
                    "view": CURRENT_VIEW_NAME,
                },
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Activated: {CURRENT_VIEW_NAME} now reads from '{physical_table}'."
            )
        )
