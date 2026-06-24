from __future__ import annotations

import pytest
from django.db import connection, transaction

from scripts import run_postgres_tests as postgres_runner


def test_postgres_config_rejects_bad_port(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("POSTGRES_TEST_PORT", "not-a-port")

    with pytest.raises(SystemExit) as exc_info:
        postgres_runner._postgres_config()

    assert exc_info.value.code == 2
    assert "POSTGRES_TEST_PORT" in capsys.readouterr().err


def test_postgres_config_derives_bounded_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_TEST_PORT", "55432")
    monkeypatch.setenv("POSTGRES_TEST_WAIT_SECONDS", "20")

    config = postgres_runner._postgres_config()

    assert config.port == 55432
    assert config.connect_timeout == 5


def test_connect_uses_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_kwargs: dict[str, object] = {}

    def fake_connect(**kwargs: object) -> None:
        captured_kwargs.update(kwargs)
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(postgres_runner.psycopg, "connect", fake_connect)
    config = postgres_runner.PostgresTestConfig(
        host="127.0.0.1",
        port=55432,
        db="classyfire",
        user="classyfire",
        password="classyfire",
        connect_timeout=3,
    )

    with pytest.raises(RuntimeError, match="stop after capture"):
        postgres_runner._connect(config)

    assert captured_kwargs["connect_timeout"] == 3


@pytest.mark.django_db
def test_harness_uses_postgresql_16_or_newer() -> None:
    assert connection.vendor == "postgresql"
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version_num")
        assert int(cursor.fetchone()[0]) >= 160000


@pytest.mark.django_db
def test_postgres_copy_from_stdin_is_available() -> None:
    connection.ensure_connection()
    raw_connection = connection.connection
    assert raw_connection is not None
    with raw_connection.cursor() as cursor:
        cursor.execute("CREATE TEMP TABLE pg_harness_copy (id integer PRIMARY KEY, name text)")
        with cursor.copy("COPY pg_harness_copy (id, name) FROM STDIN") as copy:
            copy.write_row((1, "alpha"))
        cursor.execute("SELECT name FROM pg_harness_copy WHERE id = 1")
        assert cursor.fetchone() == ("alpha",)


@pytest.mark.django_db
def test_postgres_skip_locked_is_available() -> None:
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TABLE pg_harness_lock (id integer PRIMARY KEY)")
            cursor.execute("INSERT INTO pg_harness_lock VALUES (1)")
            cursor.execute(
                "SELECT id FROM pg_harness_lock ORDER BY id FOR UPDATE SKIP LOCKED"
            )
            assert cursor.fetchone() == (1,)


@pytest.mark.django_db
def test_remote_task_partial_unique_index_exists_in_database() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                idx.indisunique,
                pg_get_expr(idx.indpred, idx.indrelid) AS predicate,
                array_agg(att.attname ORDER BY key.ord) AS indexed_columns
            FROM pg_class index_class
            JOIN pg_index idx ON idx.indexrelid = index_class.oid
            JOIN pg_class table_class ON table_class.oid = idx.indrelid
            JOIN pg_namespace namespace ON namespace.oid = table_class.relnamespace
            JOIN LATERAL unnest(idx.indkey) WITH ORDINALITY AS key(attnum, ord) ON TRUE
            JOIN pg_attribute att
              ON att.attrelid = table_class.oid
             AND att.attnum = key.attnum
            WHERE namespace.nspname = current_schema()
              AND table_class.relname = 'remote_tasks'
              AND index_class.relname = 'remote_task_active_inchikey_unique'
            GROUP BY idx.indisunique, idx.indpred, idx.indrelid
            """
        )
        row = cursor.fetchone()

    assert row is not None
    is_unique, predicate, indexed_columns = row
    assert is_unique is True
    assert predicate is not None
    assert "state" in predicate
    assert indexed_columns == ["inchikey"]
