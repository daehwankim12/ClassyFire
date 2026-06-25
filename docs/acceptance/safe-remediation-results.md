# Safe Remediation Results

Branch: `safe-production-remediation` (HEAD 456eacc)
Verified: 2026-06-25

| Check | Result | Evidence |
| --- | --- | --- |
| Ruff | pass | `uv run ruff check .` — All checks passed (controller-verified) |
| mypy | pass | `uv run mypy apps config scripts` — Success: no issues found in 142 files (controller-verified) |
| compileall | pass | `uv run python -m compileall -q apps config scripts` — OK (controller-verified) |
| migrations | pass | `uv run python manage.py makemigrations --check --dry-run` — No changes detected (controller-verified) |
| Django check | pass | `uv run python manage.py check` — System check identified 0 issues (controller-verified) |
| Focused safety tests | pass (74 passed, 0 skipped, 0 failed) | `scripts/run_postgres_tests.py --reuse-db` with the 20 focused files listed in Task 22 Step 2 |
| PostgreSQL tests | pass (747 passed, 22 skipped; 26 pre-existing infra-only errors documented; 1 new failure — see note) | `scripts/run_postgres_tests.py --reuse-db tests` — full suite |
| Docker build | not run | Docker not available on this host |
| Compose runtime | not run | Docker not available on this host |
| Proxy log redaction | not run | Docker not available on this host |
| ngrok-agent log redaction | not run | Docker not available on this host |
| Backup restore smoke | not run | Docker not available on this host |
| Image layer secret scan | not run | Docker not available on this host |
| Shared throttle multiprocess | pass | focused safety tests |
| Remote provenance matrix | pass | focused safety tests |
| Stale freshness | pass | focused safety tests |
| SDF product path | pass | focused safety tests |
| Official import source artifact | pass | focused safety tests (restore smoke not run — Docker unavailable) |
| Wishart live calls | not run | live calls are intentionally blocked for this gate |

## Pre-existing infra-only errors (26 total)

All 26 errors occur in `tests/test_restore_integrity.py` at setup: the fixture calls
`subprocess.run(['python', 'manage.py', 'migrate', '--noinput'])` against a dynamically
provisioned scratch DB, which requires a live Docker environment. These errors occur
identically on `main` (756901f) and are **not regressions**.

## New failure — test_remote_provider.py (1 test)

**`tests/test_remote_provider.py::test_wishart_entity_permanent_4xx_is_protocol_error`**

This test was written in commit `fe338b0` asserting that HTTP 422 → `EntityLookupStatus.PROTOCOL_ERROR`.
Commit `ed875fb` (Task 13) deliberately changed 4xx → `EntityLookupStatus.PERMANENT_ERROR` to create a
distinct non-retryable status, but the test assertion was not updated. The test name and assertion are
stale with respect to the new semantics: the production code behaves **correctly** per the Task 13
design (non-404 4xx must park as PERMANENT_ERROR, not be conflated with unmapped status codes that
produce PROTOCOL_ERROR). This requires the test to be updated to assert `PERMANENT_ERROR`, not a code
change.

**This is a test-assertion staleness issue, not a safety regression.**
The actual safety behavior — that 422 never triggers a calculation submit — is verified by
`tests/test_remote_entity_permanent_errors.py` (which passed in the focused suite).

## Important: incomplete verification

This branch is verified for all static, migration, and PostgreSQL checks EXCEPT the following,
which require Docker and MUST be run in CI before claiming full production safety:

- Docker build (`docker compose build`)
- Compose runtime (`docker compose up -d ...`)
- Proxy log redaction (`scripts/check_proxy_logs_for_secrets.sh`)
- ngrok-agent log redaction
- Backup restore smoke (`scripts/run_backup_restore_smoke.sh`)
- Image layer secret scan (`scripts/check_image_for_secrets.sh`)

Do NOT merge to a production release branch until these Docker/runtime/restore-smoke rows pass.
