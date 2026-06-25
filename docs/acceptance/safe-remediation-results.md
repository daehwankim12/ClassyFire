# Safe Remediation Results

Branch: `safe-production-remediation` (HEAD 899bb9c)
Verified: 2026-06-25

| Check | Result | Evidence |
| --- | --- | --- |
| Ruff | pass | `uv run ruff check .` — All checks passed (controller-verified) |
| mypy | pass | `uv run mypy apps config scripts` — Success: no issues found in 142 files (controller-verified) |
| compileall | pass | `uv run python -m compileall -q apps config scripts` — OK (controller-verified) |
| migrations | pass | `uv run python manage.py makemigrations --check --dry-run` — No changes detected (controller-verified) |
| Django check | pass | `uv run python manage.py check` — System check identified 0 issues (controller-verified) |
| Focused safety tests | pass (74 passed, 0 skipped, 0 failed) | `scripts/run_postgres_tests.py --reuse-db` with the 20 focused files listed in Task 22 Step 2 |
| PostgreSQL tests | pass (748 passed, 22 skipped, 0 new failures; 26 pre-existing infra-only errors documented) | `scripts/run_postgres_tests.py --reuse-db tests` — full suite (the 1 stale-assertion failure was fixed, see note) |
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

## RESOLVED — stale test assertion in test_remote_provider.py (1 test)

**`tests/test_remote_provider.py::test_wishart_entity_permanent_4xx_is_protocol_error`** asserted the
pre-Task-17 semantics (HTTP 422 → `PROTOCOL_ERROR`). Task 17 (commit `ed875fb`, F-10 residual)
deliberately changed non-404 4xx → `EntityLookupStatus.PERMANENT_ERROR` and updated two of the three
affected test files, but missed this one (its regression set did not include `test_remote_provider.py`).
The full suite caught it. It was a stale ASSERTION, not a safety regression — the safety behavior (422
never triggers a calculation submit) is covered by `tests/test_remote_entity_permanent_errors.py`.

**Fixed:** renamed to `test_wishart_entity_permanent_4xx_is_permanent_error` and updated the assertion
to `EntityLookupStatus.PERMANENT_ERROR`. `test_remote_provider.py` + `test_remote_entity_permanent_errors.py`
= 49 passed, ruff clean. The full suite now has **0 new failures** (748 passed, 22 skipped, 26 pre-existing
infra-only errors).

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
