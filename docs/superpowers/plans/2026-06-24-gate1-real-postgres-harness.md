# Gate 1 Real PostgreSQL Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the project’s trusted test path run through a real PostgreSQL 16 database using `uv`, with explicit smoke tests for Postgres-only behavior.

**Architecture:** Keep the app settings unchanged, but add a local/CI harness that maps `POSTGRES_TEST_*` variables to Django’s `POSTGRES_*` settings before running pytest. Provide a Compose-only path to start a disposable Postgres test database, and make the harness fail loudly if it is not talking to PostgreSQL 16.

**Tech Stack:** Python 3.12, uv, pytest, pytest-django, psycopg 3, Django 5.2, PostgreSQL 16, Docker Compose.

---

## File Structure

- Modify `pyproject.toml`: fix setuptools package discovery for `uv run --extra dev`, and add `pyyaml` for existing tests.
- Create `scripts/run_postgres_tests.py`: validate/wait for PostgreSQL 16, set Django DB env, then run pytest with the active Python interpreter.
- Create `docker-compose.test.yml`: disposable Postgres 16 service exposed only on localhost for local/CI tests.
- Modify `Makefile`: use `uv` by default and add `postgres-test-up`, `postgres-test-down`, and `test-postgres` targets.
- Create `tests/test_postgres_harness.py`: prove the active test DB is PostgreSQL, supports `COPY`, supports `FOR UPDATE SKIP LOCKED`, and exposes the real partial-unique index.
- Modify `docs/operations.md`: document the real-Postgres test workflow and the expected env vars.

## Task G1.0a: uv Packaging Baseline

**Files:**
- Modify: `pyproject.toml`

- [x] **Step 1: Reproduce failure**

Run:

```bash
UV_CACHE_DIR=/tmp/uv-cache-classyfire uv run --extra dev python -m pytest tests/test_project_boot.py -q
```

Expected before fix: fails during editable build with setuptools “Multiple top-level packages discovered”.

- [x] **Step 2: Minimal implementation**

Add `pyyaml>=6.0,<7.0` to `[project.optional-dependencies].dev`.

Add:

```toml
[tool.setuptools]
packages = {find = {include = ["apps*", "config*"], exclude = ["tests*", "deploy*", "docs*", "stubs*", "static*", "templates*"]}}
```

- [x] **Step 3: Verify green**

Run:

```bash
UV_CACHE_DIR=/tmp/uv-cache-classyfire uv run --extra dev python -m pytest tests/test_project_boot.py -q
```

Expected: `1 passed`.

## Task G1.0b: Postgres Harness Script

**Files:**
- Create: `scripts/run_postgres_tests.py`
- Test: `tests/test_postgres_harness.py`

- [x] **Step 1: Write failing smoke tests**

Create `tests/test_postgres_harness.py` with tests that:

```python
from django.db import connection, transaction


def test_harness_uses_postgresql_16_or_newer():
    assert connection.vendor == "postgresql"
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version_num")
        assert int(cursor.fetchone()[0]) >= 160000


def test_postgres_copy_from_stdin_is_available():
    connection.ensure_connection()
    raw_connection = connection.connection
    assert raw_connection is not None
    with raw_connection.cursor() as cursor:
        cursor.execute("CREATE TEMP TABLE pg_harness_copy (id integer PRIMARY KEY, name text)")
        with cursor.copy("COPY pg_harness_copy (id, name) FROM STDIN") as copy:
            copy.write_row((1, "alpha"))
        cursor.execute("SELECT name FROM pg_harness_copy WHERE id = 1")
        assert cursor.fetchone() == ("alpha",)


def test_postgres_skip_locked_is_available():
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TABLE pg_harness_lock (id integer PRIMARY KEY)")
            cursor.execute("INSERT INTO pg_harness_lock VALUES (1)")
            cursor.execute(
                "SELECT id FROM pg_harness_lock ORDER BY id FOR UPDATE SKIP LOCKED"
            )
            assert cursor.fetchone() == (1,)
```

Run without Postgres env:

```bash
UV_CACHE_DIR=/tmp/uv-cache-classyfire uv run --extra dev python -m pytest tests/test_postgres_harness.py -q
```

Expected: fails because it cannot connect to the default `db` host, or because the DB is not PostgreSQL.

- [x] **Step 2: Implement `scripts/run_postgres_tests.py`**

The script must:

- read `POSTGRES_TEST_HOST`, `POSTGRES_TEST_PORT`, `POSTGRES_TEST_DB`, `POSTGRES_TEST_USER`, `POSTGRES_TEST_PASSWORD`;
- fall back to existing `POSTGRES_*` env vars;
- default to `127.0.0.1:55432`, db/user/password `classyfire`;
- set Django’s `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`;
- set `DJANGO_SECRET_KEY=test-secret-key-not-for-prod` when absent;
- connect with psycopg and wait up to `POSTGRES_TEST_WAIT_SECONDS` seconds;
- fail if `server_version_num < 160000`;
- run `sys.executable -m pytest` with passed pytest args, defaulting to `tests`.

- [x] **Step 3: Verify script against known local DB**

Run:

```bash
UV_CACHE_DIR=/tmp/uv-cache-classyfire POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=55432 POSTGRES_TEST_DB=cf_test_wt4 POSTGRES_TEST_USER=classyfire POSTGRES_TEST_PASSWORD=classyfire uv run --extra dev python scripts/run_postgres_tests.py tests/test_postgres_harness.py -q
```

Expected: all harness tests pass and the script prints PostgreSQL 16 connection details.

## Task G1.0c: Makefile and Compose Test DB

**Files:**
- Create: `docker-compose.test.yml`
- Modify: `Makefile`

- [x] **Step 1: Create Compose test DB**

Create `docker-compose.test.yml` with a `postgres-test` service:

```yaml
services:
  postgres-test:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: ${POSTGRES_TEST_DB:-classyfire}
      POSTGRES_USER: ${POSTGRES_TEST_USER:-classyfire}
      POSTGRES_PASSWORD: ${POSTGRES_TEST_PASSWORD:-classyfire}
    ports:
      - "127.0.0.1:${POSTGRES_TEST_PORT:-55432}:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_TEST_USER:-classyfire} -d ${POSTGRES_TEST_DB:-classyfire}"]
      interval: 2s
      timeout: 5s
      retries: 30
    volumes:
      - postgres_test_data:/var/lib/postgresql/data

volumes:
  postgres_test_data:
```

- [x] **Step 2: Update `Makefile`**

Use `uv` for test commands:

```make
UV_CACHE_DIR ?= /tmp/uv-cache-classyfire
UV_RUN = UV_CACHE_DIR=$(UV_CACHE_DIR) uv run --extra dev

POSTGRES_TEST_HOST ?= 127.0.0.1
POSTGRES_TEST_PORT ?= 55432
POSTGRES_TEST_DB ?= classyfire
POSTGRES_TEST_USER ?= classyfire
POSTGRES_TEST_PASSWORD ?= classyfire

test:
	$(UV_RUN) python -m pytest

postgres-test-up:
	POSTGRES_TEST_PORT=$(POSTGRES_TEST_PORT) POSTGRES_TEST_DB=$(POSTGRES_TEST_DB) POSTGRES_TEST_USER=$(POSTGRES_TEST_USER) POSTGRES_TEST_PASSWORD=$(POSTGRES_TEST_PASSWORD) docker compose -f docker-compose.test.yml up -d postgres-test

postgres-test-down:
	POSTGRES_TEST_PORT=$(POSTGRES_TEST_PORT) docker compose -f docker-compose.test.yml down -v

test-postgres:
	POSTGRES_TEST_HOST=$(POSTGRES_TEST_HOST) POSTGRES_TEST_PORT=$(POSTGRES_TEST_PORT) POSTGRES_TEST_DB=$(POSTGRES_TEST_DB) POSTGRES_TEST_USER=$(POSTGRES_TEST_USER) POSTGRES_TEST_PASSWORD=$(POSTGRES_TEST_PASSWORD) $(UV_RUN) python scripts/run_postgres_tests.py
```

- [x] **Step 3: Verify non-Docker path**

Run against an already-running DB:

```bash
POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=55432 POSTGRES_TEST_DB=cf_test_wt4 POSTGRES_TEST_USER=classyfire POSTGRES_TEST_PASSWORD=classyfire make test-postgres
```

Expected: pytest starts through `scripts/run_postgres_tests.py` and uses PostgreSQL.

## Task G1.0d: Documentation and Gate Evidence

**Files:**
- Modify: `docs/operations.md`

- [x] **Step 1: Document the workflow**

Add a “Real PostgreSQL test harness” section documenting:

```bash
make postgres-test-up
make test-postgres
make postgres-test-down
```

Also document the external-DB path:

```bash
POSTGRES_TEST_HOST=127.0.0.1 \
POSTGRES_TEST_PORT=55432 \
POSTGRES_TEST_DB=cf_test_wt4 \
POSTGRES_TEST_USER=classyfire \
POSTGRES_TEST_PASSWORD=classyfire \
make test-postgres
```

- [x] **Step 2: Record local limitation**

State that this machine’s Docker daemon is not accessible to the current user, so local verification used the already-running PostgreSQL 16.14 instance on `127.0.0.1:55432`.

- [x] **Step 3: Final verification**

Run:

```bash
UV_CACHE_DIR=/tmp/uv-cache-classyfire uv run --extra dev python -m ruff check .
UV_CACHE_DIR=/tmp/uv-cache-classyfire uv run --extra dev python -m mypy apps config
UV_CACHE_DIR=/tmp/uv-cache-classyfire DJANGO_SECRET_KEY=test-secret-key-not-for-prod POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=55432 POSTGRES_DB=cf_test_wt4 POSTGRES_USER=classyfire POSTGRES_PASSWORD=classyfire uv run --extra dev python manage.py makemigrations --check --dry-run
POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=55432 POSTGRES_TEST_DB=cf_test_wt4 POSTGRES_TEST_USER=classyfire POSTGRES_TEST_PASSWORD=classyfire make test-postgres
```

Expected: ruff clean, mypy clean, migration check clean, and PostgreSQL test suite passing.
