#!/usr/bin/env python
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from math import ceil

import psycopg

DEFAULT_SECRET_KEY = "test-secret-key-not-for-prod"
MIN_POSTGRES_VERSION_NUM = 160000
MAX_CONNECT_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class PostgresTestConfig:
    host: str
    port: int
    db: str
    user: str
    password: str
    connect_timeout: int


def _env_value(test_name: str, django_name: str, default: str) -> str:
    return os.environ.get(test_name) or os.environ.get(django_name) or default


def _fail_config(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def _port() -> int:
    raw_value = _env_value("POSTGRES_TEST_PORT", "POSTGRES_PORT", "55432")
    try:
        port = int(raw_value)
    except ValueError:
        _fail_config(f"POSTGRES_TEST_PORT must be an integer, got {raw_value!r}")

    if not 1 <= port <= 65535:
        _fail_config(f"POSTGRES_TEST_PORT must be between 1 and 65535, got {raw_value!r}")
    return port


def _postgres_config() -> PostgresTestConfig:
    wait_seconds = _wait_seconds()
    return PostgresTestConfig(
        host=_env_value("POSTGRES_TEST_HOST", "POSTGRES_HOST", "127.0.0.1"),
        port=_port(),
        db=_env_value("POSTGRES_TEST_DB", "POSTGRES_DB", "classyfire"),
        user=_env_value("POSTGRES_TEST_USER", "POSTGRES_USER", "classyfire"),
        password=_env_value("POSTGRES_TEST_PASSWORD", "POSTGRES_PASSWORD", "classyfire"),
        connect_timeout=_connect_timeout_seconds(wait_seconds),
    )


def _wait_seconds() -> float:
    raw_value = os.environ.get("POSTGRES_TEST_WAIT_SECONDS", "30")
    try:
        wait_seconds = float(raw_value)
    except ValueError:
        _fail_config(f"POSTGRES_TEST_WAIT_SECONDS must be numeric, got {raw_value!r}")

    if wait_seconds < 1:
        _fail_config(f"POSTGRES_TEST_WAIT_SECONDS must be at least 1, got {raw_value!r}")
    return wait_seconds


def _connect_timeout_seconds(wait_seconds: float) -> int:
    return min(MAX_CONNECT_TIMEOUT_SECONDS, ceil(wait_seconds))


def _connect(config: PostgresTestConfig) -> psycopg.Connection:
    return psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=config.db,
        user=config.user,
        password=config.password,
        connect_timeout=config.connect_timeout,
    )


def _wait_for_postgres(config: PostgresTestConfig) -> int:
    deadline = time.monotonic() + _wait_seconds()
    last_error: Exception | None = None

    while True:
        try:
            with _connect(config) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SHOW server_version_num")
                    version_num = int(cursor.fetchone()[0])
                return version_num
        except psycopg.Error as exc:
            last_error = exc
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)

    print(
        "Timed out waiting for PostgreSQL at "
        f"{config.host}:{config.port}/{config.db}: {last_error}",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _export_django_database_env(config: PostgresTestConfig) -> None:
    os.environ["POSTGRES_HOST"] = config.host
    os.environ["POSTGRES_PORT"] = str(config.port)
    os.environ["POSTGRES_DB"] = config.db
    os.environ["POSTGRES_USER"] = config.user
    os.environ["POSTGRES_PASSWORD"] = config.password
    os.environ.setdefault("DJANGO_SECRET_KEY", DEFAULT_SECRET_KEY)


def main(argv: list[str] | None = None) -> int:
    pytest_args = list(argv if argv is not None else sys.argv[1:]) or ["tests"]
    config = _postgres_config()
    _export_django_database_env(config)

    version_num = _wait_for_postgres(config)
    if version_num < MIN_POSTGRES_VERSION_NUM:
        print(
            "PostgreSQL 16 or newer is required for real database tests; "
            f"server_version_num={version_num}",
            file=sys.stderr,
        )
        return 1

    print(
        "Using PostgreSQL "
        f"server_version_num={version_num} at {config.host}:{config.port}/{config.db} "
        f"as user {config.user}",
        flush=True,
    )
    command = [sys.executable, "-m", "pytest", *pytest_args]
    return subprocess.run(command, env=os.environ.copy(), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
