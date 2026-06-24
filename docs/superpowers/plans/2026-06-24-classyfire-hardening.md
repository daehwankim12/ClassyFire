# ClassyFire Batch Cache Web — Hardening Plan (Post-Review)

> Source: external code review of working tree at HEAD 71512ca (22 findings F-01..F-22 + missing tests + refactoring plan).
> Plan baseline: **commit 7323c8a** (in-progress Codex hardening checkpointed: F-02/F-03/F-04-snapshot/F-18 partial + new tests; 424 passed / 22 skipped, ruff + mypy clean).
> Execution: use `superpowers:subagent-driven-development` per gate. Each task = implement → review → fix. Gate gates: do not exit a gate with any unchecked **must** item.

## Root-cause of the misses
Original build verified with **mock providers + SQLite substitute + no Docker**. That systematically hid: real concurrency/crash safety (C), real upload paths (D), deployment boot (G), and result immutability (A). **Therefore WS-H (real-PostgreSQL CI + concurrency/crash integration tests) is the prerequisite for trusting any other fix.**

## Status legend
- [ ] not started  ·  [~] partially done in 7323c8a (verify + finish)  ·  [x] done & verified

---

## Workstreams (grouping the 22 findings)

| WS | Theme | Findings | Size |
|----|-------|----------|------|
| A | Result immutability & provenance | F-04, F-05, F-18, F-20 | large (schema) |
| B | NEED_STRUCTURE recovery + compound model + uniqueness | F-02, F-03, F-09, F-17 | medium (schema+logic) |
| C | Remote queue crash-safety + true global control | F-06, F-07, F-08, F-10 | large (distributed) |
| D | Real streaming ingest pipeline | F-01, F-11 | large (architecture) |
| E | Export scalability & lifecycle | F-12 | medium |
| F | Security & access | F-14, F-15, F-16 | medium |
| G | Ops/deploy automation, packaging, counters | F-13, F-19, F-21, F-22 | medium |
| H | Real-PostgreSQL CI + concurrency/crash/load tests | (32 missing tests) | foundational |

---

## GATE 1 — Required before ANY ngrok exposure (correctness + security blockers)

### G1.0 — WS-H minimum: real PostgreSQL test harness  [ ]
- [ ] Make CI/local tests run against a **real PostgreSQL 16** (not SQLite). The SQLite substitute hid 41 failures and cannot exercise SKIP LOCKED / partial-unique / COPY.
- [ ] Provide a documented `make test` path that provisions/uses Postgres (compose `db` service or a CI service container).
- **Acceptance:** full suite green on real Postgres; the PG-only tests (COPY/SKIP LOCKED/partial-unique) actually run (not skipped).

### G1.1 — WS-B: NEED_STRUCTURE recovery + gate + uniqueness  [~]
- [~] **F-02** key-only input no longer fixes a blank compound; later structure fills missing fields atomically *(done in 7323c8a — verify with test #6/#7/#8)*.
- [ ] **F-02** existing **active key-only RemoteTask** gets the new `compound_id` attached atomically (`UPDATE ... WHERE compound_id IS NULL`).
- [~] **F-03** `remote_enabled=False` must NOT create a remote task; structured miss → `REMOTE_DELAYED`, key-only miss → `NEED_STRUCTURE` *(done in 7323c8a — verify with test #9)*.
- [ ] **F-09** add constraints: `CompoundStructure.inchikey` UNIQUE + InChIKey regex CHECK; `BatchItem(job,row_order)` unique; `RemoteQuery(provider,remote_query_id)` unique; `RemoteQueryItem(query,task)` unique. (Pre-clean any dup data first.)
- [ ] **F-17** structure-resubmit endpoint: reject if item not `NEED_STRUCTURE` (409), reject if `resolved`/snapshot already set (409), apply `check_structure_length`, run inside `select_for_update()`.
- **Acceptance:** full NEED_STRUCTURE→add-structure→entity-miss→submit→poll→complete flow passes on real Postgres; resubmit on a resolved/non-NEED_STRUCTURE item is rejected.

### G1.2 — WS-A (snapshot tier): pin results so history/export are stable  [~]
- [~] **F-04** `BatchItem.classification_snapshot` pinned at resolution; export reads the pinned snapshot, NOT live cache *(field+migration in 7323c8a — verify export immutability test #16)*.
- [ ] **F-04** confirm EVERY resolution path pins the snapshot (live hit, seed hit, entity hit, query complete, structure-resubmit), and export never re-queries `lookup_current_classification`.
- [~] **F-18** result_writer: same source priority + **older** `retrieved_at` must NOT supersede current *(guard added in 7323c8a — verify test #17)*; rank by `(priority, retrieved_at, source_version)`.
- [ ] **F-18** result_writer entry enforces `requested_inchikey == returned_inchikey` invariant (defense-in-depth even if caller forgets).
- [ ] **F-18** ChemOnt FK only on exact CHEMONTID/ID match; drop non-unique `name .first()` fallback (preserve raw label instead).
- **Acceptance:** a job's export/result is byte-stable after the live cache is superseded; stale same-priority write is ignored; mismatch never written under requested key.

### G1.3 — WS-F: security blockers  [ ]
- [ ] **F-14** URL capability secret out of access logs: add `GET /jobs/<uuid>/claim/<secret>/` → verify hash → set `HttpOnly; Secure; SameSite=Lax` cookie → 303 to secret-less `/jobs/<uuid>/`; AND redact the secret path in Caddy/Gunicorn access logs.
- [ ] **F-15** browser shared-password UX: a CSRF-protected shared-password form (or Caddy `basic_auth` / Django login) that issues a session credential — so locked mode is usable without hand-set headers and operators aren't forced to blank the hash. Fix `.env.example` to document **Django `make_password`** (not Werkzeug).
- [ ] **F-16** job-creation rate limit + quotas: per-IP/principal job rate, max rows/job, daily row cap, concurrent active jobs, export frequency; enforce `DISK_UPLOAD_PAUSE_PERCENT` at the create path (not just stats). Proxy rate-limit + DB-backed quota.
- **Acceptance:** result links don't leak secrets to logs; a normal browser can authenticate in locked mode; abusive job creation is throttled/quota-rejected.

### G1.4 — WS-G (partial): boot automation, counters, packaging  [ ]
- [ ] **F-19** counter/terminal consistency: `PENDING_NORMALIZATION` is BLOCKING; introduce `COMPLETE_WITH_ACTION_REQUIRED` for NEED_STRUCTURE-only jobs; recompute under `select_for_update()`; consistent `completed_at`. Align `completed` definition with the BLOCKING/ACTION_REQUIRED sets.
- [ ] **F-21** add a one-shot `migrate` service (`migrate --noinput && collectstatic --noinput`); `web`/`worker` `depends_on: migrate: service_completed_successfully`; run cleanup as a scheduled service; implement upload + export retention.
- [ ] **F-22** `[tool.setuptools.packages.find] include=["apps*","config*"]`; add `pyyaml` to dev deps; prod image installs runtime deps only (no dev/Playwright); add a lockfile; verify `pip install .` succeeds and the Docker image imports the installed package.
- [ ] Host exposure: bind proxy to localhost or rely on ngrok-only egress (ngrok reaches proxy on the internal network); avoid publishing 80/443 to the host unless intended.
- **Acceptance:** `docker compose up -d` starts a working stack with NO manual migrate/collectstatic; `pip install .` works; counters never report COMPLETE while normalization/remote is pending.

**GATE 1 EXIT:** real-Postgres suite green; the 5 first tests (below) pass; a clean-machine `docker compose up` boots and serves; no secret in logs; auth usable; quotas enforced.

---

## GATE 2 — Required before real Wishart traffic / bulk user input

### G2.1 — WS-D: real streaming ingest  [ ]
- [ ] **F-01** wire CSV/TSV/SDF parsers to the real web/API upload routes via format dispatch on suffix/content-type; bounded streaming (no full `file.read()`); SDF record/property byte caps; preserve external_id + original SMILES. Add **HTTP-route** integration tests (not just parser unit tests).
- [ ] **F-11** move normalization/lookup/compound-upsert/task-create OUT of the HTTP request into a durable chunk worker; job state machine `UPLOADING→PARSING→NORMALIZING→LOCAL_LOOKUP→REMOTE_PENDING/ACTION_REQUIRED→COMPLETE`; chunk checkpoints; bulk `WHERE inchikey IN (...)` lookup + bulk upsert (kill N+1).
- **Acceptance:** 100k-row CSV/SDF ingest with bounded memory; HTTP returns immediately; restart resumes PENDING_NORMALIZATION.

### G2.2 — WS-C: remote queue crash-safety + true global control  [ ]
- [ ] **F-06** transactional submission **outbox** + deterministic idempotency key `(provider, payload_hash)`; persist submission intent BEFORE the network call; reconcile uncertain state before resubmit; finalize query + task links in one transaction.
- [ ] **F-07** lease **token + version** issued on claim; ALL transitions conditional (`WHERE lease_version=? AND lease_token=?`), raise on lost lease; heartbeat or operation-sized lease for long remote calls.
- [ ] **F-08** **shared** (Postgres/Redis) global token-bucket rate limiter + provider circuit breaker with single half-open probe; fix `runworker` `any(worker.tick() ...)` serial short-circuit → real concurrency.
- [ ] **F-10** explicit HTTP status classification (404=NOT_FOUND, 401/403=AUTH_FAILURE→stop, 408/425/429/5xx=RETRYABLE, other 4xx=PERMANENT); unknown provider status = protocol error (not COMPLETE); Retry-After HTTP-date support; reuse one `httpx.Client` (pooling); per-attempt raw saved.
- **Acceptance:** crash-after-submit does not double-submit; expired-lease stale worker cannot overwrite; N workers respect ONE global rate; concurrent poll of one query is safe.

### G2.3 — WS-A (full immutable model)  [ ]
- [ ] **F-05/F-20** append-only `ClassificationResult` / `RemoteAttempt` / `SourceArtifact`; `BatchItem.resolved_classification` FK (data-migrate from snapshot); preserve mismatch + submit + per-poll raw payloads; provenance-preserving official-import pipeline (staging→validate→immutable source event→current pointer).
- **Acceptance:** every official result is reproducible from an immutable event with raw payload + checksum; mismatch is a stored source event, not a discarded failure.

### G2.4 — WS-E: export scalability & lifecycle  [ ]
- [ ] **F-12** `QuerySet.iterator(chunk_size)` + `select_related(resolved_classification)` + streaming gzip to temp file + atomic rename; `ExportArtifact` model + persistent volume + checksum + expiry + scheduled cleanup; CSV injection: strip leading whitespace before the `= + - @` check; add audit columns (canonical SMILES, InChI, requested/returned key, query id, source version).
- **Acceptance:** large export with bounded memory; artifacts persist across container recreation; cleanup runs; injection-safe.

### G2.5 — WS-G: full backup + restore integrity  [ ]
- [ ] **F-13** dump the FULL DB excluding only the rebuildable seed (`--exclude-table=classyfire_seed*`); back up upload/export artifacts; restore test validates checksum, row counts, FK, exact lookup, and that pending tasks keep their submitted structure; fix the alpine `python3`/`stat` retention divergence.
- **Acceptance:** restore-from-backup yields a working system with intact compound structures + provenance + resumable pending tasks.

### G2.6 — WS-H full  [ ]
- [ ] Add the real-Postgres concurrency/crash/load integration tests (see Missing Tests). Include EXPLAIN ANALYZE on the seed/live lookup at scale.

**GATE 2 EXIT:** bulk ingest, remote outbox/lease/global-limiter, immutable provenance, streaming export, full backup/restore, and real-Postgres concurrency tests all in place and green.

---

## GATE 3 — Later / hardening  [ ]
- [ ] `ClassificationLookupBackend` interface (Postgres/LMDB/RocksDB swap seam).
- [ ] Metrics/tracing + dashboards (queue lag, retry/circuit/mismatch counts, rate-limit wait).
- [ ] Seed/live partitioning or read replica; S3-compatible artifact storage.
- [ ] Provider contract/schema-version validation; chaos/fault-injection; million-row load test; per-user remote budget + fair scheduling; audit-log signing.

---

## Schema / data-migration cautions (irreversible — isolate & sequence)
1. F-09 uniqueness/regex CHECKs: pre-clean duplicates/format violations FIRST.
2. F-04 snapshot field (0002, committed) → later F-05/F-20 `resolved_classification` FK needs a **backfill data migration** from the snapshot.
3. WS-A full append-only model is the largest migration — do it as ONE isolated Gate-2 task.

## First 5 tests to write (highest value)
1. `test_need_structure_then_submit_structure_and_complete_remote_calculation`
2. `test_structured_miss_does_not_create_remote_task_when_remote_disabled`
3. `test_remote_submit_crash_is_reconciled_without_duplicate_submission`
4. `test_real_upload_routes_dispatch_csv_tsv_and_sdf_streams`
5. `test_batch_item_and_export_keep_original_classification_after_live_supersede`

## Full missing-test list (32)
Real CSV/TSV/SDF upload routes; bounded-memory large files; SDF `$$$$`-missing/huge record/property; full NEED_STRUCTURE recovery; blank-compound fill; active-task structure attach; remote-disabled no-task; crash-after-submit; query-link mid-crash; lease-expiry fencing; concurrent poll of one query; shared global limiter across workers; circuit half-open single probe; export immutability after supersede; stale same-priority no-overwrite; entity/query mismatch raw saved; per-attempt raw saved; PG partial-unique + SKIP LOCKED integration; concurrent same-InChIKey upsert; concurrent counter recompute; PENDING_NORMALIZATION restart recovery; backup pending-task structure intact; backup checksum/rowcount/FK; URL secret log redaction; browser shared-password flow; job rate-limit/quota; export streaming/checksum/expiry/cleanup; leading-whitespace CSV injection; official-import checksum/conflict.

## Final recommendation (reviewer, endorsed)
**Not safe to expose via ngrok or connect to real Wishart yet.** Localhost + mock-provider demo is fine. Clear GATE 1 before ngrok; clear GATE 2 before bulk user input / real Wishart.
