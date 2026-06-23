"""Management command: audit a seed import before activation.

Checks:
  - Row count vs manifest expected_row_count
  - Duplicate InChIKey count
  - InChIKey regex/format (27 chars: 14-10-1)
  - ChemOnt FK existence for all five slots
  - Five-slot path length / coverage
  - Null coverage per slot

Writes structured report to ``SeedImport.audit_result`` and emits a
``SystemEvent`` of category ``SEED``.

Sets ``SeedImport.import_status`` to ``AUDIT_PASS`` or ``AUDIT_FAIL``.
"""
from __future__ import annotations

import json
import re
from typing import Any

import psycopg.sql as pgsql
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

INCHIKEY_RE = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")

CHEMONT_SLOTS = [
    "kingdom_id",
    "superclass_id",
    "class_id",
    "subclass_id",
    "direct_parent_id",
]


def _audit_table(
    table: str,
    expected_row_count: int | None,
) -> dict[str, Any]:
    """Run all audit checks on *table* and return a structured report."""
    report: dict[str, Any] = {"table": table, "checks": {}, "passed": True}

    tbl = pgsql.Identifier(table)

    with connection.cursor() as cur:
        # 1. Actual row count.
        cur.execute(pgsql.SQL("SELECT COUNT(*) FROM {}").format(tbl))
        actual_count: int = cur.fetchone()[0]
        report["actual_row_count"] = actual_count

        count_check: dict[str, Any] = {"actual": actual_count}
        if expected_row_count is not None:
            count_check["expected"] = expected_row_count
            count_check["ok"] = actual_count == expected_row_count
            if not count_check["ok"]:
                report["passed"] = False
        else:
            count_check["ok"] = True
        report["checks"]["row_count"] = count_check

        # 2. Duplicate InChIKey count.
        cur.execute(
            pgsql.SQL("SELECT COUNT(*) - COUNT(DISTINCT inchikey) FROM {}").format(tbl)
        )
        dup_count: int = cur.fetchone()[0]
        report["duplicate_count"] = dup_count
        report["checks"]["duplicates"] = {"count": dup_count, "ok": dup_count == 0}
        if dup_count > 0:
            report["passed"] = False

        # 3. InChIKey format check.
        cur.execute(
            pgsql.SQL(
                "SELECT COUNT(*) FROM {} "
                "WHERE inchikey !~ '^[A-Z]{{14}}-[A-Z]{{10}}-[A-Z]$'"
            ).format(tbl)
        )
        bad_inchikey: int = cur.fetchone()[0]
        report["checks"]["inchikey_format"] = {
            "bad_count": bad_inchikey,
            "ok": bad_inchikey == 0,
        }
        if bad_inchikey > 0:
            report["passed"] = False

        # 4. ChemOnt FK existence.
        fk_checks: dict[str, Any] = {}
        for slot in CHEMONT_SLOTS:
            cur.execute(
                pgsql.SQL(
                    """
                    SELECT COUNT(*) FROM {} s
                    WHERE s.{} IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM chemont_nodes c WHERE c.numeric_id = s.{}
                      )
                    """
                ).format(tbl, pgsql.Identifier(slot), pgsql.Identifier(slot))
            )
            bad_fk: int = cur.fetchone()[0]
            fk_checks[slot] = {"bad_count": bad_fk, "ok": bad_fk == 0}
            if bad_fk > 0:
                report["passed"] = False
        report["checks"]["chemont_fk"] = fk_checks

        # 5. Five-slot path coverage.
        cur.execute(
            pgsql.SQL("SELECT COUNT(*) FROM {} WHERE kingdom_id IS NOT NULL").format(tbl)
        )
        kingdom_count: int = cur.fetchone()[0]

        cur.execute(
            pgsql.SQL(
                "SELECT COUNT(*) FROM {} "
                "WHERE kingdom_id IS NOT NULL "
                "AND superclass_id IS NOT NULL "
                "AND class_id IS NOT NULL "
                "AND subclass_id IS NOT NULL "
                "AND direct_parent_id IS NOT NULL"
            ).format(tbl)
        )
        full_path_count: int = cur.fetchone()[0]

        path_pct = (full_path_count / actual_count * 100) if actual_count else 0.0
        kingdom_pct = (kingdom_count / actual_count * 100) if actual_count else 0.0
        report["checks"]["path_coverage"] = {
            "full_path_count": full_path_count,
            "kingdom_count": kingdom_count,
            "total": actual_count,
            "full_path_pct": round(path_pct, 2),
            "kingdom_pct": round(kingdom_pct, 2),
            "ok": True,  # Coverage is informational, not a hard fail.
        }

        # 6. Null coverage per slot.
        null_checks: dict[str, Any] = {}
        for slot in CHEMONT_SLOTS:
            cur.execute(
                pgsql.SQL("SELECT COUNT(*) FROM {} WHERE {} IS NULL").format(
                    tbl, pgsql.Identifier(slot)
                )
            )
            null_count: int = cur.fetchone()[0]
            null_pct = (null_count / actual_count * 100) if actual_count else 0.0
            null_checks[slot] = {
                "null_count": null_count,
                "null_pct": round(null_pct, 2),
            }
        report["checks"]["null_coverage"] = null_checks

    return report


class Command(BaseCommand):
    help = "Audit a seed import and write results to SeedImport.audit_result."

    def add_arguments(self, parser):
        parser.add_argument(
            "--seed-import-id",
            type=int,
            default=None,
            help="SeedImport PK to audit.  Defaults to the latest IMPORTED row.",
        )
        parser.add_argument(
            "--table",
            default=None,
            help="Physical table to audit (defaults to classyfire_seed).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            default=False,
            help="Print full report as JSON.",
        )

    def handle(self, *args, **options):
        from apps.core.enums import EventCategory
        from apps.operations.models import SeedImport, SystemEvent

        seed_import_id = options["seed_import_id"]
        seed_import: SeedImport
        if seed_import_id:
            try:
                seed_import = SeedImport.objects.get(pk=seed_import_id)
            except SeedImport.DoesNotExist as exc:
                raise CommandError(f"SeedImport {seed_import_id} not found") from exc
        else:
            candidate = (
                SeedImport.objects.filter(import_status="IMPORTED")
                .order_by("-created_at")
                .first()
            )
            if not candidate:
                raise CommandError("No IMPORTED SeedImport rows found.")
            seed_import = candidate

        table = options["table"] or seed_import.activated_table or "classyfire_seed"

        self.stdout.write(f"Auditing table {table} (SeedImport pk={seed_import.pk})...")

        report = _audit_table(table, seed_import.expected_row_count)
        report["seed_import_id"] = seed_import.pk

        # Save results.
        existing = seed_import.audit_result or {}
        existing.update(report)
        seed_import.audit_result = existing
        seed_import.actual_row_count = report["actual_row_count"]
        seed_import.duplicate_count = report["duplicate_count"]
        seed_import.import_status = "AUDIT_PASS" if report["passed"] else "AUDIT_FAIL"
        seed_import.save(
            update_fields=[
                "audit_result",
                "actual_row_count",
                "duplicate_count",
                "import_status",
            ]
        )

        # Emit SystemEvent.
        SystemEvent.objects.create(
            category=EventCategory.SEED,
            event_type="seed_audit",
            payload={
                "seed_import_id": seed_import.pk,
                "table": table,
                "passed": report["passed"],
                "actual_row_count": report["actual_row_count"],
            },
        )

        if options["json"]:
            self.stdout.write(json.dumps(report, indent=2))
        else:
            status = "PASS" if report["passed"] else "FAIL"
            style = self.style.SUCCESS if report["passed"] else self.style.ERROR
            self.stdout.write(
                style(
                    f"Audit {status}: {report['actual_row_count']} rows, "
                    f"{report['duplicate_count']} dupes."
                )
            )
            for check_name, check_val in report["checks"].items():
                ok = check_val.get("ok", True) if isinstance(check_val, dict) else True
                marker = "OK" if ok else "FAIL"
                self.stdout.write(f"  [{marker}] {check_name}: {check_val}")
