# Gate 1 Subagent Workflow Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to execute this plan. Each task gets an implementer, then a spec-compliance reviewer, then a code-quality reviewer. Do not run implementation workers in parallel unless their write sets are disjoint.

**Goal:** Clear Gate 1 blockers before ngrok exposure by grounding all verification in a real PostgreSQL harness first.

**Architecture:** G1.0 is the trust anchor and must land before other Gate 1 workers start. After G1.0, split work by subsystem: compound/queue correctness, result snapshot correctness, browser/security controls, and ops/counters/packaging.

**Tech Stack:** Django 5.2, PostgreSQL 16, pytest/pytest-django, uv, Docker Compose, psycopg 3.

---

## Gate 1 Dependency Graph

1. **G1.0 Real PostgreSQL Harness**: must finish first.
2. **G1.1 Data correctness** and **G1.2 Result snapshot correctness**: can run in parallel after G1.0, but both touch batch resolution paths; reviewers must check conflicts carefully.
3. **G1.3 Security blockers**: can run in parallel with G1.1/G1.2 if its worker owns only auth, URL claim, quota, and proxy docs/config.
4. **G1.4 Ops/counters/boot**: can run in parallel with G1.3; counter changes may overlap with G1.1/G1.2 batch status code, so start after those if conflict risk is high.
5. **G1.final Gate review**: run full real-Postgres suite, lint, typecheck, migration drift, compose config, and targeted security checks.

## Subagent Execution Rules

- One implementer per task.
- Each implementer receives exact owned files and must not edit outside them without reporting `NEEDS_CONTEXT`.
- After every implementer: run spec-compliance reviewer first, then code-quality reviewer.
- Implementation workers do not commit; controller commits after review because `.git` writes require escalation.
- All verification commands use `UV_CACHE_DIR=/tmp/uv-cache-classyfire uv run --extra dev ...`.
- Real DB verification uses:

```bash
POSTGRES_TEST_HOST=127.0.0.1 \
POSTGRES_TEST_PORT=55432 \
POSTGRES_TEST_DB=cf_test_wt4 \
POSTGRES_TEST_USER=classyfire \
POSTGRES_TEST_PASSWORD=classyfire
```

## Task G1.0: Real PostgreSQL Harness

**Status:** in progress by worker `Turing`.

**Owned files:**
- `pyproject.toml`
- `scripts/run_postgres_tests.py`
- `docker-compose.test.yml`
- `Makefile`
- `tests/test_postgres_harness.py`
- `docs/operations.md`
- `docs/superpowers/plans/2026-06-24-gate1-real-postgres-harness.md`

**Acceptance:**
- `uv run --extra dev` works from a clean worktree.
- Harness refuses non-PostgreSQL and PostgreSQL < 16.
- PG-only smoke tests cover COPY, `FOR UPDATE SKIP LOCKED`, and partial unique index presence.
- `make test-postgres` runs through the harness against the real local PostgreSQL 16.14 instance.

## Task G1.1: Compound and Remote Task Correctness

**Findings:** F-02, F-03, F-09, F-17.

**Owned files:**
- `apps/compounds/models.py`
- `apps/batches/models.py`
- `apps/batches/services.py`
- `apps/batches/views.py`
- `apps/batches/api.py`
- `apps/remote/models.py`
- migrations under `apps/compounds/migrations/`, `apps/batches/migrations/`, `apps/remote/migrations/`
- tests in `tests/test_batch_ingest.py`, `tests/test_batch_ui.py`, `tests/test_remote_queue.py`, `tests/test_model_contracts.py`

**Implementation checklist:**
- [ ] Key-only input must not create or freeze a blank `CompoundStructure`.
- [ ] Structure submission must atomically fill a missing structure for the same full InChIKey.
- [ ] Existing active key-only `RemoteTask` must receive `compound_id` with `UPDATE ... WHERE compound_id IS NULL`.
- [ ] `remote_enabled=False` must never create a remote task.
- [ ] Add uniqueness/check constraints: `CompoundStructure.inchikey`, `BatchItem(job,row_order)`, `RemoteQuery(provider,remote_query_id)`, `RemoteQueryItem(query,task)`, and InChIKey format where currently missing.
- [ ] Structure resubmit endpoints must reject non-`NEED_STRUCTURE`, already resolved/snapshotted items, and oversized structures.

**Required tests:**
- `test_need_structure_then_submit_structure_and_complete_remote_calculation`
- `test_key_only_remote_task_gets_structure_attached_later`
- `test_structured_miss_does_not_create_remote_task_when_remote_disabled`
- `test_structure_resubmit_rejects_non_need_structure_item`
- `test_real_postgres_constraints_reject_duplicate_query_item`

## Task G1.2: Stable Batch Results and Result Writer Guardrails

**Findings:** F-04 snapshot tier, F-18.

**Owned files:**
- `apps/batches/models.py`
- `apps/batches/services.py`
- `apps/batches/exports.py`
- `apps/classifications/services.py`
- `apps/classifications/result_writer.py`
- `apps/remote/fanout.py`
- migrations under `apps/batches/migrations/`
- tests in `tests/test_exports.py`, `tests/test_classification_lookup.py`, `tests/test_remote_queue.py`, `tests/test_seed_import.py`

**Implementation checklist:**
- [ ] `BatchItem.classification_snapshot` exists and is populated for live hit, seed hit, entity hit, query completion, and structure-resubmit completion paths.
- [ ] Export reads pinned snapshot only for resolved rows; fallback to live lookup is allowed only for legacy rows without snapshots.
- [ ] Export output shape matches requested `ClassyFy Status` CSV columns.
- [ ] Same source priority plus older/same `retrieved_at` cannot overwrite newer live cache.
- [ ] `result_writer` rejects or preserves mismatch when requested and returned InChIKey differ.
- [ ] ChemOnt FK lookup must use exact stable ID; no non-unique name `.first()` fallback.

**Required tests:**
- `test_batch_item_and_export_keep_original_classification_after_live_supersede`
- `test_every_resolution_path_pins_classification_snapshot`
- `test_equal_priority_older_result_does_not_overwrite_newer_live`
- `test_result_writer_rejects_returned_key_mismatch`
- `test_chemont_name_collision_does_not_link_wrong_node`

## Task G1.3: Ngrok-Blocking Security Controls

**Findings:** F-14, F-15, F-16.

**Owned files:**
- `apps/core/security.py`
- `apps/core/views.py`
- `apps/core/urls.py` if present
- `apps/batches/views.py`
- `apps/batches/api.py`
- `config/urls.py`
- `deploy/caddy/Caddyfile`
- `.env.example`
- templates related to auth/access
- tests in `tests/test_security.py`, `tests/test_batch_ui.py`, `tests/e2e/test_ngrok_flow.py`

**Implementation checklist:**
- [ ] Add `/jobs/<uuid>/claim/<secret>/` exchange route that validates secret, sets an HttpOnly job-access cookie, and redirects to secret-less `/jobs/<uuid>/`.
- [ ] Redact or avoid logging secret-bearing claim paths in proxy/app access logs.
- [ ] Add CSRF-protected shared-password browser form or equivalent Caddy Basic Auth path.
- [ ] Fix `.env.example` to document Django `make_password`.
- [ ] Enforce job creation quotas: per-IP/principal rate, max rows/job, daily row cap, active job cap, export frequency, and disk high-water create rejection.

**Required tests:**
- `test_job_claim_sets_cookie_and_redirects_to_secretless_url`
- `test_secret_path_is_not_rendered_after_claim`
- `test_shared_password_form_authenticates_browser_session`
- `test_job_create_rate_limit_rejects_abuse`
- `test_disk_high_water_blocks_job_create`

## Task G1.4: Ops, Counters, and Boot Automation

**Findings:** F-19, F-21, remaining F-22.

**Owned files:**
- `apps/batches/models.py`
- `apps/batches/services.py`
- `apps/operations/cleanup.py`
- management commands under `apps/operations/management/commands/`
- `docker-compose.yml`
- `Dockerfile`
- `Makefile`
- `README.md`
- `docs/operations.md`
- tests in `tests/test_operations.py`, `tests/test_batch_ingest.py`, `tests/test_deployment.py`

**Implementation checklist:**
- [ ] Job status must not be `COMPLETE` while `PENDING_NORMALIZATION`, `PENDING_CLASSIFICATION`, or `REMOTE_DELAYED` exists.
- [ ] Introduce or emulate `COMPLETE_WITH_ACTION_REQUIRED` for jobs whose remaining terminal rows are `NEED_STRUCTURE`.
- [ ] Recompute denormalized counters under `select_for_update()`.
- [ ] Reset or set `completed_at` consistently when status moves between blocking and terminal states.
- [ ] Add one-shot `migrate` service and make `web`/`worker` depend on it.
- [ ] Add cleanup service/command for upload/export retention.
- [ ] Ensure Docker production install uses runtime deps only, while developer workflows use `uv --extra dev`.

**Required tests:**
- `test_pending_normalization_keeps_job_processing`
- `test_need_structure_only_job_is_action_required_not_plain_complete`
- `test_recompute_counters_locks_job_row`
- `test_compose_web_and_worker_wait_for_migrate`
- `test_cleanup_removes_expired_exports_and_uploads`

## G1.final: Gate Review

**Owned files:** none; review and verification only.

**Commands:**

```bash
UV_CACHE_DIR=/tmp/uv-cache-classyfire uv run --extra dev python -m ruff check .
UV_CACHE_DIR=/tmp/uv-cache-classyfire uv run --extra dev python -m mypy apps config
UV_CACHE_DIR=/tmp/uv-cache-classyfire DJANGO_SECRET_KEY=test-secret-key-not-for-prod POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=55432 POSTGRES_DB=cf_test_wt4 POSTGRES_USER=classyfire POSTGRES_PASSWORD=classyfire uv run --extra dev python manage.py makemigrations --check --dry-run
POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=55432 POSTGRES_TEST_DB=cf_test_wt4 POSTGRES_TEST_USER=classyfire POSTGRES_TEST_PASSWORD=classyfire make test-postgres
docker compose config
```

**Exit criteria:**
- All Gate 1 required tests pass on real PostgreSQL.
- No migration drift.
- `ruff` and `mypy` clean.
- `docker compose config` valid.
- Secret-bearing URL route is not required after claim.
- Browser locked-mode auth works without manual headers.
- Job quotas and disk high-water checks enforce at create time.
