# Acceptance Test Results

This document records evidence for every acceptance ID defined in
`tests/test_acceptance_matrix.py`.  Automated IDs reference the test module
and function.  Manual IDs document the procedure and the reason automation is
not feasible in the standard CI environment.

---

## T-01 Mixed Input (SMILES/InChI/InChIKey) Preserves Order and Duplicates

**Status:** Automated  
**File:** `tests/test_batch_ingest.py`  
**Tests:**
- `test_100_row_mixed_input_preserves_order_and_count`
- `test_duplicate_rows_preserved`

Verifies that a 100-row input with mixed SMILES, InChI, and InChIKey rows is
stored with `row_order` matching the original position, and that duplicate rows
are preserved (not deduplicated at ingest time).

---

## T-02 RDKit Identity (Canonical SMILES/InChIKey Computed by RDKit)

**Status:** Automated  
**File:** `tests/test_input_normalization.py`  
**Tests:**
- `test_smiles_aspirin`
- `test_inchi_aspirin`
- `test_inchikey_bare`
- `test_chiral_inchikey_differs_from_achiral`
- `test_chiral_canonical_smiles_preserves_stereo`
- `test_deuterated_differs_from_protio`
- `test_deuterated_inchikey_fixed`
- `test_salt_inchikey_differs_from_acid_alone`
- `test_salt_canonical_smiles_contains_both_components`

All expected InChIKey and canonical SMILES values are hard-coded from the
project venv's RDKit (2026.03.3).  Tests fail if RDKit produces different
values, ensuring the normalization pipeline is RDKit-driven (not hard-coded
business logic).

---

## T-03 Seed Lookup (Local Cache Hit)

**Status:** Automated  
**File:** `tests/test_classification_lookup.py`  
**Tests:**
- `test_exact_seed_lookup_hit`

Inserts a row into the seed cache and verifies that a lookup returns the seed
result without a remote call.

---

## T-04 Live Precedence (Live Over Seed)

**Status:** Automated  
**File:** `tests/test_classification_lookup.py`  
**Tests:**
- `test_live_over_seed_precedence`

Inserts both a seed result and a live result for the same InChIKey.  Verifies
that the live result is returned (tier = LIVE).

---

## T-05 Need Structure (InChIKey-Only Miss → NEED_STRUCTURE)

**Status:** Automated  
**File:** `tests/test_batch_ingest.py`  
**Tests:**
- `test_inchikey_only_miss_remote_disabled_sets_need_structure`

Submits an InChIKey that has no seed or live classification and no structure.
With `remote_enabled=False`, confirms the item transitions to `NEED_STRUCTURE`.

---

## T-06 Mismatch Guard (Submitted Structure InChIKey Must Match Expected)

**Status:** Automated  
**File:** `tests/test_batch_ui.py`  
**Tests:**
- `test_structure_form_mismatching_inchikey_shows_error`
- `test_api_structure_key_mismatch_leaves_need_structure`

Submits a structure whose computed InChIKey does not match the item's expected
InChIKey.  Verifies:
1. The UI form shows a mismatch error.
2. The API returns HTTP 422.
3. The item remains `NEED_STRUCTURE` (not permanently stranded).

---

## T-07 Global Dedupe (One RemoteTask per InChIKey Across Jobs)

**Status:** Automated  
**File:** `tests/test_batch_ingest.py`  
**Tests:**
- `test_global_dedupe_one_active_remote_task`

Creates two jobs for the same InChIKey with `remote_enabled=True`.  Verifies
that only one `RemoteTask` is created, not two.

---

## T-08 Worker Restart/Stale-Lock Recovery

**Status:** Automated  
**File:** `tests/test_remote_queue.py`  
**Tests:**
- `test_stale_lock_recovery_resets_to_ready`

Simulates a stale lock (locked_at timestamp older than the stale threshold).
Verifies that the recovery pass resets the task status to `READY` so a new
worker can claim it.

---

## T-09 Remote Failure Backoff/Circuit Breaker

**Status:** Automated  
**File:** `tests/test_remote_queue.py`  
**Tests:**
- `test_retryable_error_schedules_retry_with_backoff`
- `test_circuit_opens_after_consecutive_failures`

Verifies that:
1. A retryable remote error schedules a retry with exponential backoff.
2. Repeated consecutive failures open the circuit breaker, deferring subsequent
   tasks without making remote calls.

---

## T-10 Result Fan-Out (All Waiting Items Updated)

**Status:** Automated  
**File:** `tests/test_remote_queue.py`  
**Tests:**
- `test_fan_out_updates_all_waiting_items_across_jobs`

Verifies that when a remote classification result arrives for an InChIKey, all
`BatchItem` rows across all jobs that were waiting on that InChIKey are updated
to the resolved status.

---

## T-11 Export (Dup/Order/Source/Injection)

**Status:** Automated  
**File:** `tests/test_exports.py`  
**Tests:**
- `test_row_order_preserved`
- `test_duplicate_rows_preserved`
- `test_source_fields_present`
- `test_injection_escaping`

Verifies:
1. Export rows appear in original row order.
2. Duplicate input rows appear as separate export rows.
3. Source/provenance fields are present in the export.
4. Special characters (formula injection, CSV injection) are properly escaped.

---

## T-12 Seed Resume (Checkpoint Resume No Duplicates)

**Status:** Automated  
**File:** `tests/test_seed_import.py`  
**Tests:**
- `test_checkpoint_resume_no_duplicates`

Interrupts a seed import mid-stream, resumes from checkpoint, and verifies
that no duplicate rows are inserted.

---

## T-13 Seed Swap (Seed Activation/New Version)

**Status:** Automated  
**File:** `tests/test_seed_import.py`  
**Tests:**
- `test_seed_activate_view_serves_rows`
- `test_seed_activate_view_survives_multiple_calls`

Verifies:
1. After activation, the seed cache serves classification rows.
2. Calling activate multiple times is idempotent (no errors, no duplicates).

---

## T-14 Backup Restore (Backup Creates Manifest + Restore Smoke Test)

**Status:** Automated  
**File:** `tests/test_operations.py`  
**Tests:**
- `test_backup_creates_manifest_and_event`
- `test_restore_smoke_test_logs_event`

Verifies:
1. The backup command creates a manifest file listing backed-up tables and
   records a system event.
2. The restore smoke test reads the manifest, confirms it is valid, and logs a
   system event.

---

## T-15 External URL E2E (ngrok Real URL) {#t-15-external-url-e2e}

**Status:** Manual  
**Reason:** Requires a live ngrok domain (`NGROK_AUTHTOKEN` + `NGROK_DOMAIN` in
`.env`) and a running application instance reachable at that domain.  Cannot be
automated in CI without real internet connectivity and an ngrok account.

**Automated proxy test:** `tests/e2e/test_ngrok_flow.py` — exercises the same
submit → status → items → export flow using Django's test client, without
requiring a real domain.

**Manual procedure:**

1. Configure `.env`:
   ```
   NGROK_AUTHTOKEN=<your_token>
   NGROK_DOMAIN=<your_domain>.ngrok-free.app
   ```
2. Start the stack: `docker compose up -d`
3. Confirm the domain resolves: `curl -I https://<your_domain>.ngrok-free.app/`
4. Open `https://<your_domain>.ngrok-free.app/` in a browser.
5. Submit `CC(=O)Oc1ccccc1C(=O)O` (Aspirin SMILES) via the submit form.
6. Verify the result page shows classification data.
7. Download a CSV export and inspect rows.
8. Test the API using `curl`:
   ```bash
   curl -X POST https://<domain>/api/jobs \
     -H "Content-Type: application/json" \
     -d '{"text_input":"CC(=O)Oc1ccccc1C(=O)O","remote_enabled":false}'
   ```

**Expected outcome:** HTTP 201 response with `job_id` and `result_secret`.

---

## NFR-P01 p95 Lookup < 200 ms {#nfr-p01-p95-lookup}

**Status:** Automated  
**File:** `tests/test_classification_lookup.py`  
**Tests:**
- `test_p95_latency_under_200ms`

Runs 100 cached lookups and asserts that the 95th-percentile latency is under
200 ms.

---

## NFR-P02 10k Local Batch {#nfr-p02-10k-local-batch}

**Status:** Manual  
**Reason:** Requires a pre-loaded 10 000-row seed cache (FULL seed profile) and
a 10 000-row input file.  The seed dataset is not bundled in the test
environment.  Running this test would exceed the acceptable CI timeout (~60 s+).

**Manual procedure:**

1. Import the FULL seed profile:
   ```bash
   make seed-download
   make seed-verify
   make seed-import
   make seed-activate
   ```
2. Generate a 10 000-row input file:
   ```bash
   python -c "
   import csv, sys
   w = csv.writer(sys.stdout)
   for i in range(10000):
       w.writerow([f'InChIKey-{i:05d}-UHFFFAOYSA-N'])
   " > /tmp/10k_input.txt
   ```
3. POST the file to the API and measure wall-clock time.
4. Confirm the job completes and result counts are correct.

**Expected outcome:** All 10 000 rows processed; wall-clock time under 60 s
on a standard laptop.

---

## NFR-P03 100k Streaming Upload {#nfr-p03-100k-streaming-upload}

**Status:** Manual  
**Reason:** Requires generating a 100 000-row input file and profiling memory
usage during upload.  Not suitable for automated CI.

**Manual procedure:**

1. Generate a 100 000-row file.
2. Stream-upload via the API.
3. Monitor memory usage (should not exceed 2× peak file size).
4. Confirm the job accepts the upload without OOM errors.
5. For database plan evidence, run the Gate 3 dry-run first, then execute it
   only against a disposable or staging database:
   ```bash
   PGHOST=127.0.0.1 PGPORT=55432 PGDATABASE=classyfire_probe PGUSER=classyfire \
     uv run --locked scripts/gate3_load_probe.py --rows 1000000 --dry-run
   PGHOST=127.0.0.1 PGPORT=55432 PGDATABASE=classyfire_probe PGUSER=classyfire \
     uv run --locked scripts/gate3_load_probe.py --rows 1000000 --execute \
     | tee gate3-load-probe.log
   ```

---

## NFR-P04 10 Concurrent Browsers {#nfr-p04-10-concurrent-browsers}

**Status:** Manual  
**Reason:** Requires a browser fleet (Playwright/Selenium) and a running
application instance.  Not available in the standard CI environment.

**Manual procedure:**

1. Start the application stack.
2. Run Playwright with 10 parallel browser contexts submitting jobs.
3. Verify all 10 jobs complete successfully.
4. Check the stats page for correct aggregate counts.

---

## NFR-P05 Remote-Outage Local Survival

**Status:** Automated  
**File:** `tests/test_remote_queue.py`  
**Tests:**
- `test_circuit_open_defers_task_local_lookup_survives`

Opens the circuit breaker (simulating a remote outage).  Verifies that:
1. The remote task is deferred (no remote calls made).
2. The local lookup service still resolves cached compounds correctly.
