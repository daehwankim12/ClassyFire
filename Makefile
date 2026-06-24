UV_CACHE_DIR ?= /tmp/uv-cache-classyfire
UV_RUN = UV_CACHE_DIR=$(UV_CACHE_DIR) uv run --locked
UV_DEV_RUN = $(UV_RUN) --extra dev

POSTGRES_TEST_HOST ?= 127.0.0.1
POSTGRES_TEST_PORT ?= 55432
POSTGRES_TEST_DB ?= classyfire
POSTGRES_TEST_USER ?= classyfire
POSTGRES_TEST_PASSWORD ?= classyfire
PYTEST_ARGS ?= tests

.PHONY: test lint format typecheck seed-download seed-verify seed-import seed-audit seed-activate postgres-test-up postgres-test-down test-postgres

test:
	$(UV_DEV_RUN) python -m pytest

lint:
	$(UV_DEV_RUN) python -m ruff check .

format:
	$(UV_DEV_RUN) python -m ruff format .

typecheck:
	$(UV_DEV_RUN) python -m mypy apps config

seed-download:
	$(UV_RUN) python manage.py seed_download

seed-verify:
	$(UV_RUN) python manage.py seed_verify

seed-import:
	$(UV_RUN) python manage.py seed_import --resume

seed-audit:
	$(UV_RUN) python manage.py seed_audit

seed-activate:
	$(UV_RUN) python manage.py seed_activate

postgres-test-up:
	POSTGRES_TEST_PORT=$(POSTGRES_TEST_PORT) POSTGRES_TEST_DB=$(POSTGRES_TEST_DB) POSTGRES_TEST_USER=$(POSTGRES_TEST_USER) POSTGRES_TEST_PASSWORD=$(POSTGRES_TEST_PASSWORD) docker compose -f docker-compose.test.yml up -d postgres-test

postgres-test-down:
	POSTGRES_TEST_PORT=$(POSTGRES_TEST_PORT) docker compose -f docker-compose.test.yml down -v

test-postgres:
	POSTGRES_TEST_HOST=$(POSTGRES_TEST_HOST) POSTGRES_TEST_PORT=$(POSTGRES_TEST_PORT) POSTGRES_TEST_DB=$(POSTGRES_TEST_DB) POSTGRES_TEST_USER=$(POSTGRES_TEST_USER) POSTGRES_TEST_PASSWORD=$(POSTGRES_TEST_PASSWORD) $(UV_DEV_RUN) python scripts/run_postgres_tests.py $(PYTEST_ARGS)
