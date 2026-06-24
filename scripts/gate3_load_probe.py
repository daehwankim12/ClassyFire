#!/usr/bin/env python
"""Generate or run the Gate 3 manual load-probe SQL.

Default mode is dry-run: print deterministic SQL and the explicit PostgreSQL
environment-derived psql command, but do not insert rows or execute EXPLAIN.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence

DEFAULT_ROWS = 1_000_000


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def db_config_from_env(env: Mapping[str, str]) -> dict[str, str]:
    """Read only explicit PostgreSQL env vars for the manual probe."""
    return {
        "host": env.get("PGHOST", "127.0.0.1"),
        "port": env.get("PGPORT", "55432"),
        "database": env.get("PGDATABASE", "classyfire"),
        "user": env.get("PGUSER", "classyfire"),
    }


def psql_command(config: Mapping[str, str]) -> list[str]:
    return [
        "psql",
        "-h",
        config["host"],
        "-p",
        config["port"],
        "-U",
        config["user"],
        "-d",
        config["database"],
        "-v",
        "ON_ERROR_STOP=1",
    ]


def build_probe_sql(rows: int, *, explain_only: bool = False) -> str:
    explain = """\
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT status, count(*)
FROM gate3_probe_items
GROUP BY status
ORDER BY status;

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT inchikey
FROM gate3_probe_items
WHERE status = 'PENDING_CLASSIFICATION'
ORDER BY priority DESC, row_order
LIMIT 1000;
"""
    if explain_only:
        return explain

    return f"""\
SET statement_timeout = '30min';
SET lock_timeout = '5s';

CREATE TEMP TABLE gate3_probe_items (
    row_order integer PRIMARY KEY,
    inchikey char(27) NOT NULL,
    status text NOT NULL,
    priority integer NOT NULL DEFAULT 0
) ON COMMIT DROP;

INSERT INTO gate3_probe_items (row_order, inchikey, status, priority)
SELECT
    gs,
    'RDHQFKQIGNGIED-UHFFFAOYSA-N',
    CASE WHEN gs % 10 = 0 THEN 'PENDING_CLASSIFICATION' ELSE 'FOUND_SEED_CACHE' END,
    gs % 5
FROM generate_series(1, {rows}) AS gs;

ANALYZE gate3_probe_items;

{explain}"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=_positive_int, default=DEFAULT_ROWS)
    parser.add_argument("--explain-only", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    mode.add_argument("--execute", dest="dry_run", action="store_false")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = db_config_from_env(os.environ)
    command = psql_command(config)
    sql = build_probe_sql(args.rows, explain_only=args.explain_only)

    print("Command:")
    print(" ".join(command))
    print()
    print("SQL:")
    print(sql)

    if args.dry_run:
        print()
        print("Dry run only; pass --execute to run this probe.")
        return 0

    completed = subprocess.run(
        command,
        input=sql,
        text=True,
        check=False,
        env=os.environ.copy(),
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
