# Production Safety Gate

Commit `756901f` remains `NOT SAFE` until every row in this matrix is green.

| Gate | Required Evidence | Blocking Findings |
| --- | --- | --- |
| Remote submit exactly-once | Accepted-response-loss provider test shows one outbound POST across repeated ticks and restarts | F-06, C-01 |
| Remote stale-worker fencing | Actual poll path stale worker cannot write query state, attempt outcome, classification event, live pointer, batch item status, or task DONE | F-07, C-02 |
| Ingest side-effect fencing | Mid-chunk reclaim race leaves usage, budget, remote task, and item transitions exactly once | H-01 |
| Safe backup defaults | Empty, current-directory, root, backup-parent, and symlink-to-root artifact paths fail before archive creation | H-02 |
| Durable upload staging | Web/API routes persist raw upload artifact and return before parsing rows or creating `BatchItem` rows | H-04 |
| Provider auth stop | 401/403 block all workers until explicit operator reset | H-03 |
| Remote backpressure | Production requires positive per-principal budget and global queue high-water gates | H-05 |
| Runtime secret logs | Docker Compose traffic produces zero plaintext sentinel secrets in app, proxy, worker, and ngrok-agent logs. If ngrok-agent is not run, ngrok public readiness is not passed. | C-03, M-01 |
| Runtime restore | Fresh DB and fresh volumes restore DB, media, export, source artifacts, official-import source bytes, and an HTTP export download | H-02, M-01, F-20 |
| Release workflow | CI runs lint, typecheck, migrations, PostgreSQL race tests, Docker image scan, proxy log grep, and restore smoke | M-01 |

## Strict re-review remediation (2026-06-26)

Closes the `dist/ClassyFire-review-evidence-7dbc45e/ClassyFire-strict-rereview-7dbc45e.md`
findings R-01…R-09:

| Finding | Fix | Local verification |
| --- | --- | --- |
| R-01 regrouped-member repost | `RemoteSubmissionMember` per-member live reservation | `tests/test_remote_ambiguous_submit_provider.py` (Postgres) |
| R-02 stale entity side effects | `_with_owned_claim` fences every entity-first write + submit | `tests/test_remote_side_effect_fencing.py` (Postgres) |
| R-06/R-09 freshness + provenance | `request_started_at`→`submitted_at`; `request_structure_hash` on every submit attempt | `tests/test_remote_stale_freshness.py`, `tests/test_remote_provenance_contract.py` (Postgres) |
| R-03 secret log leak | `SecretRedactionFilter` redacts claim/legacy path secrets | `tests/test_security.py` |
| R-04/R-08 uploads backup/restore + path safety | shell realpath guards + dedicated uploads archive (fail-closed restore) | `tests/test_backup_safe_paths.py`, `tests/test_backup_artifact_manifest.py` |
| R-07 orphan upload artifacts | rollback compensation + retention unlink | `tests/test_upload_artifact_spooling.py` (Postgres) |
| R-05 release gate evidence | complete `.env` + sentinel-derived hash; `--secret-file`; smoke via `docker compose run --rm web` | `tests/test_release_gate_scripts.py` |

**Docker/ngrok evidence still required before beta / public / production.** The
runtime gates (`proxy-log-redaction`, `backup-restore-smoke`) only run on a
Docker-capable host or CI; this remediation was implemented and verified on a
non-Docker host. Record the green `release-gates` CI run URL and its
`compose.log` / generated-secrets artifact names here once it completes.
