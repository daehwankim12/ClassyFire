from __future__ import annotations

from scripts.gate3_load_probe import (
    build_probe_sql,
    db_config_from_env,
    main,
    psql_command,
)


def test_gate3_load_probe_builds_deterministic_sql():
    sql = build_probe_sql(123)

    assert "generate_series(1, 123)" in sql
    assert "CREATE TEMP TABLE gate3_probe_items" in sql
    assert "EXPLAIN (ANALYZE, BUFFERS, VERBOSE)" in sql


def test_gate3_load_probe_explain_only_skips_insert_sql():
    sql = build_probe_sql(123, explain_only=True)

    assert "EXPLAIN (ANALYZE, BUFFERS, VERBOSE)" in sql
    assert "INSERT INTO gate3_probe_items" not in sql


def test_gate3_load_probe_uses_explicit_pg_env():
    config = db_config_from_env(
        {
            "PGHOST": "db.example",
            "PGPORT": "15432",
            "PGDATABASE": "classyfire_probe",
            "PGUSER": "probe_user",
        }
    )

    assert psql_command(config) == [
        "psql",
        "-h",
        "db.example",
        "-p",
        "15432",
        "-U",
        "probe_user",
        "-d",
        "classyfire_probe",
        "-v",
        "ON_ERROR_STOP=1",
    ]


def test_gate3_load_probe_default_is_dry_run(capsys):
    assert main(["--rows", "42"]) == 0
    output = capsys.readouterr().out

    assert "generate_series(1, 42)" in output
    assert "Dry run only; pass --execute to run this probe." in output
