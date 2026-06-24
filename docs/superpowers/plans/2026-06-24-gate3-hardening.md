# Gate 3 Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Gate 3 hardening seams for lookup backend replacement, operations metrics, artifact storage, provider contract validation, remote budget fairness, signed audit logs, and load-test evidence.

**Architecture:** Keep the current Django/PostgreSQL implementation as the default runtime. Add narrow interfaces and DB-backed policy/observability layers without changing the public API contract or replacing Gate 2's proven queue/export/provenance flows. Each workstream must land with real PostgreSQL tests and preserve existing behavior by default.

**Tech Stack:** Python 3.12, Django 5.2, PostgreSQL 16, uv, pytest/pytest-django, ruff, mypy, Docker Compose config validation.

---

## File Structure

- `apps/classifications/backends.py`: classification lookup backend protocol and PostgreSQL implementation.
- `apps/classifications/services.py`: route single/bulk lookup calls through the configured backend.
- `apps/operations/metrics.py`: queue/export/provider metric snapshot collection with JSON-serializable output.
- `apps/operations/management/commands/emit_metrics_snapshot.py`: CLI for metrics snapshots.
- `apps/batches/storage.py`: artifact storage protocol and local filesystem implementation.
- `apps/batches/exports.py`: use the storage protocol for export writes/reads while keeping local paths as default.
- `apps/remote/contract.py`: provider payload/schema validation helpers and contract error types.
- `apps/remote/wishart.py`: validate provider responses before parsing success states.
- `apps/remote/models.py`: remote budget and audit signature models.
- `apps/remote/budget.py`: per-principal budget reservation and fair-priority helpers.
- `apps/remote/queue.py` / `apps/remote/worker.py`: apply budget/fair scheduling where tasks are claimed or created.
- `apps/operations/audit.py`: signed SystemEvent payload helper.
- `scripts/gate3_load_probe.py`: bounded million-row/load/EXPLAIN probe script with dry-run defaults.
- Tests:
  - `tests/test_lookup_backend.py`
  - `tests/test_gate3_metrics.py`
  - `tests/test_artifact_storage.py`
  - `tests/test_provider_contract.py`
  - `tests/test_remote_budget.py`
  - `tests/test_audit_signing.py`
  - `tests/test_gate3_load_probe.py`

## Task 1: Lookup Backend Seam

**Files:**
- Create: `apps/classifications/backends.py`
- Modify: `apps/classifications/services.py`
- Test: `tests/test_lookup_backend.py`

- [ ] **Step 1: Write failing tests**

Add tests proving:
- `lookup_current_classification()` delegates to a configured backend.
- `lookup_current_classifications()` delegates to the same backend and preserves current result shape.
- The default backend is PostgreSQL and existing seed/live behavior remains unchanged.

Run:

```bash
POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=55432 POSTGRES_TEST_DB=cf_test_wt12 POSTGRES_TEST_USER=classyfire POSTGRES_TEST_PASSWORD=classyfire UV_CACHE_DIR=/tmp/uv-cache-classyfire uv run --locked --extra dev python scripts/run_postgres_tests.py --reuse-db tests/test_lookup_backend.py -q
```

Expected: fails because `apps.classifications.backends` does not exist or services do not delegate.

- [ ] **Step 2: Implement backend protocol**

Create `ClassificationLookupBackend` with `lookup_one(inchikey)` and `lookup_many(inchikeys)`. Implement `PostgresClassificationLookupBackend` by moving the current SQL/selectors orchestration behind the interface.

- [ ] **Step 3: Route service calls**

Add `get_lookup_backend()` in `services.py`, defaulting to PostgreSQL. Keep public dataclass/result types stable.

- [ ] **Step 4: Verify**

Run the focused lookup tests, then `tests/test_classification_lookup.py` and `tests/test_batch_ingest.py`.

- [ ] **Step 5: Commit**

Commit message: `feat: add classification lookup backend seam`.

## Task 2: Metrics Snapshot and CLI

**Files:**
- Create: `apps/operations/metrics.py`
- Create: `apps/operations/management/commands/emit_metrics_snapshot.py`
- Modify: `docs/operations.md`
- Test: `tests/test_gate3_metrics.py`

- [ ] **Step 1: Write failing tests**

Add tests proving the snapshot includes:
- remote queue counts by state and lag seconds for due work;
- retry/circuit/rate-limit counters from DB-backed provider control and attempts;
- mismatch counts from immutable classification results;
- export artifact counts and bytes;
- JSON CLI output.

Expected: fails because `collect_metrics_snapshot()` and command are missing.

- [ ] **Step 2: Implement metrics collector**

Use aggregate queries only; do not iterate full tables. Return a plain dataclass with `to_dict()`.

- [ ] **Step 3: Implement command**

`python manage.py emit_metrics_snapshot --json` writes one JSON document to stdout.

- [ ] **Step 4: Verify**

Run `tests/test_gate3_metrics.py tests/test_operations.py`.

- [ ] **Step 5: Commit**

Commit message: `feat: add gate3 metrics snapshots`.

## Task 3: Artifact Storage Seam

**Files:**
- Create: `apps/batches/storage.py`
- Modify: `apps/batches/exports.py`
- Modify: `config/settings/base.py`
- Test: `tests/test_artifact_storage.py`

- [ ] **Step 1: Write failing tests**

Add tests proving:
- exports are saved/read through `ArtifactStorage`;
- local filesystem implementation preserves current paths/checksums;
- unsupported S3 backend config fails closed with a clear configuration error rather than silently using local storage.

Expected: fails because storage seam is absent.

- [ ] **Step 2: Implement storage protocol**

Create `ArtifactStorage` protocol with `open_write_atomic(name)`, `open_read(name)`, `exists(name)`, `delete(name)`, and `path_for_local_read(name)`.

- [ ] **Step 3: Wire exports**

Keep `EXPORT_DIR` local default. Existing `FileResponse` path behavior remains unchanged for local storage.

- [ ] **Step 4: Verify**

Run `tests/test_artifact_storage.py tests/test_exports.py`.

- [ ] **Step 5: Commit**

Commit message: `feat: add artifact storage seam`.

## Task 4: Provider Contract Validation and Fault Injection

**Files:**
- Create: `apps/remote/contract.py`
- Modify: `apps/remote/wishart.py`
- Modify: `apps/remote/mock.py`
- Test: `tests/test_provider_contract.py`

- [ ] **Step 1: Write failing tests**

Add tests proving:
- unknown `classification_status` is a protocol error, not complete;
- malformed entity/query response records a retryable or permanent protocol outcome according to status;
- Retry-After HTTP-date remains supported;
- mock provider can inject malformed schema for worker tests.

Expected: fails because contract validators and fault injection flags are absent.

- [ ] **Step 2: Implement validators**

Validate only fields the application consumes. Preserve raw payload in attempts/results.

- [ ] **Step 3: Wire provider**

Call validators before returning success/complete statuses.

- [ ] **Step 4: Verify**

Run `tests/test_provider_contract.py tests/test_remote_provider.py tests/test_remote_queue.py`.

- [ ] **Step 5: Commit**

Commit message: `feat: validate remote provider contracts`.

## Task 5: Remote Budget, Fair Scheduling, and Signed Audit

**Files:**
- Modify: `apps/remote/models.py`
- Create migration: `apps/remote/migrations/0005_remote_budget_audit.py`
- Create: `apps/remote/budget.py`
- Modify: `apps/batches/services.py`
- Modify: `apps/remote/queue.py`
- Create: `apps/operations/audit.py`
- Modify: `apps/operations/admin_views.py`
- Test: `tests/test_remote_budget.py`
- Test: `tests/test_audit_signing.py`

- [ ] **Step 1: Write failing tests**

Add tests proving:
- a principal daily remote budget can reject new remote work without dropping the batch item;
- tasks from different principals are claimed fairly when priorities are equal;
- signed audit payloads verify and tampering fails verification.

Expected: fails because budget and signed audit helpers are absent.

- [ ] **Step 2: Add DB model/migration**

Add `RemoteBudgetLedger` rows for reservations and `AuditSignature` rows linked to `SystemEvent`.

- [ ] **Step 3: Implement reservation and fair priority**

Default budget is permissive so existing tests keep passing. When configured, reserve one unit per new remote task candidate and mark excess rows `REMOTE_DELAYED` with a clear message.

- [ ] **Step 4: Sign audit events**

Use HMAC-SHA256 with `DJANGO_SECRET_KEY` or `AUDIT_SIGNING_KEY`; store key id and payload digest.

- [ ] **Step 5: Verify**

Run `tests/test_remote_budget.py tests/test_audit_signing.py tests/test_batch_ingest.py tests/test_remote_queue.py`.

- [ ] **Step 6: Commit**

Commit message: `feat: add remote budget fairness and audit signing`.

## Task 6: Load Probe and Documentation

**Files:**
- Create: `scripts/gate3_load_probe.py`
- Modify: `docs/operations.md`
- Modify: `docs/acceptance-test-results.md`
- Test: `tests/test_gate3_load_probe.py`

- [ ] **Step 1: Write failing tests**

Add tests proving the script can generate deterministic dry-run SQL/EXPLAIN commands without inserting a million rows by default.

- [ ] **Step 2: Implement script**

Support `--dry-run`, `--rows`, `--explain-only`, and explicit DB env usage. Default is dry-run.

- [ ] **Step 3: Document manual runbook**

Record that actual million-row execution is manual evidence and explain expected artifacts.

- [ ] **Step 4: Verify**

Run `tests/test_gate3_load_probe.py`.

- [ ] **Step 5: Commit**

Commit message: `test: add gate3 load probe`.

## Final Verification

- [ ] Run full real-Postgres suite:

```bash
POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=55432 POSTGRES_TEST_DB=cf_test_wt12 POSTGRES_TEST_USER=classyfire POSTGRES_TEST_PASSWORD=classyfire UV_CACHE_DIR=/tmp/uv-cache-classyfire make test-postgres PYTEST_ARGS='--create-db -q'
```

- [ ] Run static checks:

```bash
UV_CACHE_DIR=/tmp/uv-cache-classyfire uv run --locked --extra dev ruff check .
UV_CACHE_DIR=/tmp/uv-cache-classyfire uv run --locked --extra dev mypy apps config
UV_CACHE_DIR=/tmp/uv-cache-classyfire uv run --locked --extra dev python manage.py makemigrations --check --dry-run
```

- [ ] Validate compose:

```bash
cp .env.example .env
docker compose config >/tmp/classyfire-gate3-compose-config.out
rm .env
```

- [ ] Final review:
  - Confirm all Gate 3 bullets in `docs/superpowers/plans/2026-06-24-classyfire-hardening.md` are implemented or explicitly documented as manual evidence.
  - Confirm no public API contract regression.
  - Confirm worktree is clean except intended commits.
