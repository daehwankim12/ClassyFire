# Seedless On-Demand Caching Server — Design

**Date:** 2026-06-30
**Status:** Implemented on branch `seedless-cache` (full migration reset complete; verified via embedded-PG16 catalog diff + suite).

## Context & Problem

ClassyFire currently bulk-imports a ~73.1M-row precomputed **seed** (the Zenodo
"enriched" InChIKey→ChemOnt TSV) into the `classyfire_seed` table, exposed via the
`classyfire_seed_current` view and layered *under* live results in the
`classyfire_current` lookup view (live-over-seed via a `NOT EXISTS` clause).

The seed **source data is corrupt**, and the importer faithfully loads it:

- 16,761 rows carry the exact garbage tree `[1,423,453,554,554]` =
  *Inorganic compounds / Homogeneous non-metal / Other non-metal organides /
  Other non-metal oxides*, assigned to clearly-organic compounds (bile acids,
  terpenoids, …).
- Proof it is garbage, not a lookup bug: the same connectivity skeleton
  `BHQCQFFYRZLCQQ` has some stereoisomers tagged `[1,423,453,554,554]` (inorganic)
  and others `[0,12,258,1445,1104]` (Organic / Lipids / Steroids / Bile acids). A
  skeleton cannot be both.
- Evidence file: `seed_cache/classyfire_DH_compare.xlsx` (the `classyfire_DH`
  sheet = seed-read output; it matches the corrupt rows). 158 of 972 sampled rows
  differ from the authoritative classification.

**Decision:** abandon the bulk seed entirely. Convert ClassyFire into a **seedless
on-demand caching server**: lookups serve only the live cache (`classyfire_live`);
cache misses are classified on demand via the existing asynchronous Wishart remote
path, and the result is cached.

## Goals / Non-goals

**Goals**
- Remove the seed **read tier** so the seed can never satisfy a lookup.
- Fully remove the seed stack (table, model, import code, admin, stats, settings,
  dead enum/column surfaces).
- Keep the live-cache, remote-classification, official-import, and batch paths
  unchanged — they already populate and serve the cache a seedless server needs.
- Clean **migration reset** to a seed-free baseline; fresh alpha deploy.

**Non-goals**
- Changing the remote protocol, rate limiting, or batch semantics.
- Preserving existing seed or batch data (alpha; DB is wiped — confirmed by user).

**Implementation principle:** deletion-focused. Minimize new code — the change is
predominantly *removal* of the seed stack plus a one-line view simplification
(dropping the seed UNION branch). Avoid introducing logic that isn't strictly
required by the removal.

**Work split:**
- **This machine — code + tests + full migration reset.** Apply all seedless code
  changes, perform the clean migration RESET (collapse every app to a single
  `0001_initial`, re-encoding non-model SQL), and verify (embedded PG16 catalog
  diff + full suite). This is the deliverable here.
- **Deploy machine — `migrate` on a fresh DB + deploy.** Nothing more than running
  the reset baseline against an empty database; no per-app history to replay.

## Behavior change (the only one)

Redefine the `classyfire_current` view as **live-only**: drop the tier-2
`classyfire_seed_current` UNION branch and its `NOT EXISTS` gate. Every lookup then
reads `classyfire_live` exclusively. A miss flows through the **unchanged**
asynchronous entity-first remote path:

1. **GET** `/entities/{inchikey}` (rate bucket `entity`) → `FOUND` →
   `write_live_result(source_type=WISHART_LIVE_ENTITY)` → cached. No POST.
2. `NOT_FOUND`/`MISS` **with** a structure → **POST** `/queries` (rate bucket
   `submit`, the server's 12 POST/min cap) → poll **GET** `/queries/{id}` →
   `write_live_result(source_type=WISHART_LIVE_QUERY)`.
3. `MISS` **without** a structure → `NEED_STRUCTURE` (structure resolution first).

**Throughput note.** Wishart endpoints verified against
`bitbucket.org/wishartlab/classyfire_api` (POST `/queries`, GET `/queries/{id}`,
GET `/entities/{inchikey}`) and the `/access` policy (12 **POST**/min, server-side,
POST-only). A POST batches structures as a TSV `query_input`
(`label<TAB>structure` per line; `wishart.py:298`), so submission is **already
batched, not one-per-POST**. The batch is clamped to `_effective_batch_size()` =
`min(REMOTE_BATCH_SIZE, 10)` because Wishart paginates the completed `/queries`
JSON at **10 compounds/page** and `poll_query` fetches only page 1 — submitting
more would silently strand entities past page 1 (`worker.py:134-142`); a poll
response declaring `number_of_pages > 1` fails loudly (`contract.py:151`), so
truncation can never pass silently. (Pagination mechanics — `?page=N`,
`number_of_pages`, `number_of_elements`, 10 compounds/page — verified against the
official `/access` docs; current handling already matches.) Submission is
paced at `REMOTE_SUBMITS_PER_MIN`=10/min — within the official **12 POST/min** limit
(see the compliance section) — so effective submission throughput = 10/min × ≤10
structures = **≤100 new structures/min**. Compounds already in Wishart's store
resolve via the cheaper GET entity lookup (≤120/min, separate bucket, **no POST
cost**), so the POST ceiling binds **only for compounds new to Wishart** — softening
the bottleneck for a seedless server.

**Out of scope (future optimization).** Larger submit batches (e.g. 50/POST →
~500/min at 10 submits/min, still within the 12 POST/min limit) are possible *only*
by first extending `poll_query` to fetch **all** pages —
`/queries/{id}.json?page=1..number_of_pages`, cross-checking `number_of_elements` —
then raising `REMOTE_BATCH_SIZE` above 10. This is
correctness-sensitive (the 10-cap exists precisely to avoid stranding entities
beyond page 1) and is deliberately kept separate from the seed removal. Tracked as a
follow-up unless the user folds it in.

## Invariants preserved (no change)

`classyfire_live` (immutable `ClassificationResult` event log + current-pointer
arbitration in `result_writer.write_live_result`), `official_import`, and the
entire `apps/remote` write-back path are untouched. They already fill the live
cache identically for remote and official-import results.

## Wishart API limit compliance

**Binding limit:** the official ClassyFire access policy states explicitly —
*"Please limit your POST requests to 12 per minute."* That stated **12 POST/min** is
the authoritative ceiling (POST-only; no documented GET limit). The `classyfire_api`
client gem's pacing (`sleep(20)`≈3/min between submits, `sleep(0.2)` between GET
retrievals, `slice_length`, `initial_nr_of_jobs=2`, `sleeping_time=60`) is that
example client's **conservative self-pacing**, not a stated requirement — useful as a
sanity reference, but the binding number is 12 POST/min.

**Policy:** keep every client-side limit ≤ the official stated/server limits.
Current settings, checked against them:

| Operation | Official / server limit | Our setting | Compliant? |
|---|---|---|---|
| POST submit rate | **12/min** (stated policy) | `REMOTE_SUBMITS_PER_MIN`=10 | ✅ 10 ≤ 12 (safe margin) |
| Structures per POST | server JSON paginates 10/page | `REMOTE_BATCH_SIZE`=10 (single-page poll caps at 10) | ✅ |
| GET retrieval rate | none stated (client `sleep(0.2)`≈5/s as politeness) | `REMOTE_ENTITY_REQUESTS_PER_MIN`=120 (2/s) | ✅ well under |
| Poll spacing | client `sleeping_time=60` (example) | `REMOTE_POLL_MIN_SECONDS`=60 | ✅ |
| Concurrency | client `initial_nr_of_jobs=2` (example) | `REMOTE_MAX_CONCURRENCY`=2 | ✅ |
| Base URL | `http://classyfire.wishartlab.com` | `WISHART_BASE_URL` (same) | ✅ |

**No config change required** — all settings already sit within the official 12
POST/min limit (10/min leaves a safe margin) and the server's 10/page submission
shape. The `rate_limit.py` comments already cite the 12/min cap correctly; the
`sleep(20)`/3-min client pacing is conservative example code, not a binding target.

## Scope: full removal inventory

| Area | Removed |
|---|---|
| View / DB | `classyfire_current` → live-only; `classyfire_seed_current` view; `classyfire_seed` table + `ClassyfireSeed` model; `operations.SeedImport` model/table |
| Import stack | `seed_stream.py`, `seed_manifest.py`; commands `seed_download` / `seed_import` / `seed_audit` / `seed_activate` / `seed_verify` |
| Lookup code | `services.py` `_seed_row_to_result`, `LookupTier.SEED`, tier dispatch → live-only; `selectors.py` comments |
| Batch | `batches/services.py` `FOUND_SEED_CACHE` branches + `recompute_job_counters` seed counting; `fanout.py` `FOUND_STATUSES` |
| Enum / column | `BatchItemStatus.FOUND_SEED_CACHE`; `BatchJob.seed_hit_count`; `SeedProfile`; `EventCategory.SEED` |
| Surfaces | `batches/views.py`·`api.py`·`exports.py`·`models.py` `seed_hit_count`; `admin.py` `ClassyfireSeedAdmin`; `operations/stats.py` `seed_count` |
| Settings / misc | `SEED_PROFILE`, `SEED_RECORD_ID`, `SEED_AUTO_UPDATE`; `backup.py` seed exclusion + dependency note; `cleanup.py` docstring; delete `sql/current_lookup.sql` |

## Migration strategy: full RESET (done on this machine)

Performed here, not deferred: every app's migration history is collapsed to a
single clean `0001_initial` reflecting the seedless model state — **50 migration
files → 8**. No create-then-drop of seed objects; a fresh `migrate` builds the
final schema directly.

1. Strip seed from the model/code layer (inventory above); deletion-focused.
2. Delete every `apps/*/migrations/0*.py` (keep each `__init__.py`); `makemigrations`
   → fresh `0001_initial` per app. Final `db_table` names, Meta
   `constraints`/`indexes`, and FKs regenerate automatically; historical renames
   (`*_rename_*`) and RunPython backfills collapse away.
3. Re-encode the non-model SQL as `RunSQL`: the **live-only** `classyfire_current`
   view (`classifications/0002_classyfire_current_view`, depends on the
   classifications + chemont initials) and the two raw `classyfire_live` expression
   indexes (`live_priority_retrieved_idx`, `live_inchikey14_idx`). Model-Meta CHECK
   constraints (len-27 InChIKey, source-artifact bytes/key) regenerate from Meta.
   **Do not** recreate `classyfire_seed` / `classyfire_seed_current`.
4. The view carries **no vestigial columns**: `tier` / `seed_source_id` /
   `seed_version` were dropped with the seed era (nothing reads them), and
   `LookupTier` + `ClassificationLookupResult.tier` are gone too. `SourceType`
   no longer carries the now-unproduced `HISTORICAL_AGGREGATE`.
5. Deploy machine simply runs `migrate` on an empty DB; no per-app history to
   replay, no seed download/import/activate in bootstrap.

**Schema-fidelity check (done here).** Catalog diff (tables / columns / view
definitions / index + constraint defs via `pg_catalog`) of the reset build vs the
pre-reset seedless build: identical except the intentionally-dropped vestigial view
columns. Full suite: **826 pass, 0 non-remote failures** (the remote failures are
pre-existing/environmental to the embedded-PG test harness — no schema errors).

## Deploy

- Remove `seed_download`/`seed_import`/`seed_activate` from any deploy/bootstrap
  script or docs.
- `migrate` on an empty DB.
- `runworker` + `process_jobs` run as before; cache misses populate
  `classyfire_live` live (entity GET first, POST submit on genuine miss).

## Testing

**Verified on this machine (embedded PG16).**
- Deleted seed-specific tests; adapted live-cache/remote tests off the seed tier.
- Static gates: `ruff`, `mypy` (no new errors), `python manage.py check`,
  `makemigrations --check` (clean — 8-file baseline).
- Code-level behavior: `services._current_row_to_result` is live-only; a cache miss
  still enqueues a `RemoteTask`.
- Schema: the reset migration set applies on a fresh DB; `classyfire_current` is
  live-only; catalog diff vs the pre-reset seedless build is clean.
- Full suite: 826 pass, 0 non-remote failures. The remote failures are
  pre-existing/environmental to the embedded-PG harness (no schema errors); they
  are out of scope for this machine.

**Deploy machine.** `migrate` the baseline on an empty DB; run `runworker` +
`process_jobs`. Cache misses populate `classyfire_live` (entity GET first, POST
submit on genuine miss).

## Risks

- **Custom-SQL fidelity.** The reset must faithfully re-encode every view / raw
  index / CHECK / function; a missed one silently changes the schema. Mitigated by
  the `pg_dump --schema-only` diff above.
- **Stray references.** `EventCategory.SEED`, `SeedProfile`, `SeedImport`,
  `FOUND_SEED_CACHE`, `seed_hit_count` may have residual usages. Grep-verify zero
  references before deleting each.
- **Language-agnostic boundary.** Keep the DB-centric state machine intact (per
  project constraint) — the view + the immutable-event cache remain the portable
  contract; only the seed tier leaves.
