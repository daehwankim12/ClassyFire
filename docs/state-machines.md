# State Machines & Queue Protocol

This document is the **language-agnostic specification** for the stateful
parts of ClassyFire Batch Cache Web. The Django models in `apps/` are the
current implementation, but **all state lives in PostgreSQL as explicit
columns** so that any individual service may later be reimplemented in Go or
Rust against the *same* schema. A port MUST honour the enum string values, the
DB constraints, and the transitions described here.

Ground rules (binding):

- Every enum value is a stable **UPPERCASE string** (`models.TextChoices`).
  Never use ordinals/IntEnum, never pickle. The persisted value is the literal
  string (e.g. `"READY"`).
- Enums and their allowed-state **sets** are defined once, in
  [`apps/core/enums.py`](../apps/core/enums.py). This document mirrors them.
- Invariants are enforced by **real DB constraints** (CHECK / partial UNIQUE),
  not just Python.
- Timestamps are `timestamptz` (`DateTimeField`, `USE_TZ = True`).

---

## 1. `batch_items` — user-facing item status

Enum: `BatchItemStatus` (requirements §6.2). Column: `batch_item.status`.

Each uploaded row moves through a normalization → lookup → (optional remote) →
result lifecycle. Duplicates and input order are preserved via
`batch_item.row_order` (per-job 0-based position; duplicates allowed).

### States

| State | Meaning | Terminal? |
| --- | --- | --- |
| `PENDING_NORMALIZATION` | Raw input not yet parsed/normalized. | no |
| `FOUND_LIVE_CACHE` | Resolved from the current live cache (`classyfire_live`). | yes |
| `FOUND_SEED_CACHE` | Resolved from the immutable seed (`classyfire_seed`). | yes |
| `FOUND_WISHART_ENTITY` | Resolved via a Wishart entity lookup. | yes |
| `PENDING_CLASSIFICATION` | Normalized; awaiting (local or remote) classification. | no |
| `COMPLETE_AFTER_WISHART` | Completed after a remote Wishart classification. | yes |
| `NEED_STRUCTURE` | Only an InChIKey was given and no structure is known; cannot classify remotely. | yes* |
| `REMOTE_DELAYED` | Remote work is outstanding/queued; result not yet available. | no |
| `INVALID_INPUT` | Input could not be parsed into a valid structure/key. | yes |
| `RESULT_KEY_MISMATCH` | The remote result's InChIKey did not match the requested one. | yes |

\* `NEED_STRUCTURE` is terminal for the current job but a later upload of the
structure can produce a fresh item.

### Allowed transitions

```
PENDING_NORMALIZATION
    ├─▶ INVALID_INPUT                 (parse failure)
    ├─▶ NEED_STRUCTURE               (InChIKey-only, no known structure, remote needs structure)
    ├─▶ FOUND_LIVE_CACHE             (hit in classyfire_live)
    ├─▶ FOUND_SEED_CACHE             (hit in classyfire_seed)
    ├─▶ FOUND_WISHART_ENTITY         (hit via Wishart entity lookup)
    └─▶ PENDING_CLASSIFICATION       (no cache hit; must classify)

PENDING_CLASSIFICATION
    ├─▶ REMOTE_DELAYED               (remote task enqueued / in flight)
    └─▶ FOUND_LIVE_CACHE             (concurrent fill landed in cache)

REMOTE_DELAYED
    ├─▶ COMPLETE_AFTER_WISHART       (remote classification succeeded)
    ├─▶ RESULT_KEY_MISMATCH          (returned key ≠ requested key)
    └─▶ INVALID_INPUT                (remote rejected the structure)
```

Terminal states (`FOUND_*`, `COMPLETE_AFTER_WISHART`, `NEED_STRUCTURE`,
`INVALID_INPUT`, `RESULT_KEY_MISMATCH`) are not left again within the same job.

---

## 2. `remote_tasks` — the work queue (`TaskState`)

Enum: `TaskState` (requirements §6.3). Column: `remote_task.state`.

`remote_tasks` **is** the language-agnostic queue. Its claim columns are the
protocol:

| Column | Type | Role |
| --- | --- | --- |
| `state` | text | Lifecycle state (this machine). |
| `priority` | integer | Higher = claimed first. |
| `attempt` | integer | Retry counter (starts at 0). |
| `next_run_at` | timestamptz null | Earliest time the task may be claimed (backoff/defer). |
| `lock_owner` | text null | Worker id currently holding the task. |
| `locked_at` | timestamptz null | When the current lock was taken (for lease expiry). |

### Active vs terminal

`DONE` is the **only terminal** state. Every other state is **active** and
holds the per-InChIKey dedup slot. `active_states()` in `apps/core/enums.py` is
the single source of truth and is what feeds the partial-unique index:

```
active = { READY, LOCKED, RUNNING, ENTITY_LOOKUP, SUBMIT_WAIT,
           SUBMITTED, POLL_WAIT, RETRY_WAIT, DEFERRED }
terminal = { DONE }
```

**Design choice — `DEFERRED` is active.** A task parked for later still
represents outstanding work for that InChIKey, so it must keep holding the
dedup slot; otherwise a duplicate task could be created alongside it. We
therefore include `DEFERRED` in the active set.

### Global dedupe invariant (DB-enforced)

> At most ONE active `remote_task` may exist per `inchikey`.

Enforced by a **partial unique index**, not Python:

```sql
CREATE UNIQUE INDEX "remote_task_active_inchikey_unique"
  ON "remote_task" ("inchikey")
  WHERE "state" IN ('READY','LOCKED','RUNNING','ENTITY_LOOKUP','SUBMIT_WAIT',
                    'SUBMITTED','POLL_WAIT','RETRY_WAIT','DEFERRED');
```

A second active insert for the same InChIKey raises `IntegrityError`; a `DONE`
row never conflicts. The exact-length InChIKey is also DB-enforced:

```sql
ALTER TABLE "remote_task"
  ADD CONSTRAINT "remote_task_inchikey_len27" CHECK (LENGTH("inchikey") = 27);
```

### States

| State | Meaning |
| --- | --- |
| `READY` | Eligible to be claimed (subject to `next_run_at`). |
| `LOCKED` | Claimed by a worker (`lock_owner`/`locked_at` set), not yet executing. |
| `RUNNING` | Worker is actively processing. |
| `ENTITY_LOOKUP` | Doing a Wishart entity lookup phase. |
| `SUBMIT_WAIT` | Waiting on the submit rate limiter before submitting. |
| `SUBMITTED` | Query submitted upstream; awaiting a `remote_query` id/poll. |
| `POLL_WAIT` | Waiting for the next poll window (`next_run_at`). |
| `RETRY_WAIT` | Transient failure; backing off before retry (`attempt`++). |
| `DEFERRED` | Parked (e.g. circuit open / quota exhausted); still dedup-holding. |
| `DONE` | Terminal: work finished (success or permanent failure). |

### Allowed transitions

```
READY ─▶ LOCKED ─▶ RUNNING
                     ├─▶ ENTITY_LOOKUP ─▶ SUBMIT_WAIT
                     └─▶ SUBMIT_WAIT
SUBMIT_WAIT ─▶ SUBMITTED ─▶ POLL_WAIT ─▶ RUNNING        (poll due → resume)
RUNNING ─▶ DONE                                         (success / permanent failure)

# failure / control edges (from any active processing state)
RUNNING|ENTITY_LOOKUP|SUBMIT_WAIT|SUBMITTED|POLL_WAIT ─▶ RETRY_WAIT
RETRY_WAIT ─▶ READY                                     (when next_run_at elapses)
* (any active) ─▶ DEFERRED                              (circuit open / quota)
DEFERRED ─▶ READY                                       (when next_run_at elapses)
LOCKED|RUNNING ─▶ READY                                 (lease expired / unlock; lock cleared)
```

`attempt` increments on each entry to `RETRY_WAIT`. On a max-attempt exhaustion
the task goes to `DONE` (recording permanent failure), freeing the dedup slot.

### Queue claim protocol (`FOR UPDATE SKIP LOCKED`)

Any worker, in any language, claims work atomically and lock-free:

```sql
-- 1) Claim: pick the highest-priority due, unlocked task and lock it.
WITH claimed AS (
    SELECT id
    FROM remote_task
    WHERE state = 'READY'
      AND (next_run_at IS NULL OR next_run_at <= now())
    ORDER BY priority DESC, next_run_at NULLS FIRST, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE remote_task t
   SET state = 'LOCKED',
       lock_owner = %(worker_id)s,
       locked_at = now(),
       updated_at = now()
  FROM claimed
 WHERE t.id = claimed.id
RETURNING t.*;
```

- `FOR UPDATE SKIP LOCKED` lets N workers drain concurrently without blocking
  each other; each row is handed to exactly one worker.
- The `remote_task_claim_idx` on `(state, priority, next_run_at)` backs the
  selection.
- **Lease expiry / crash recovery:** a sweeper resets stuck rows whose
  `locked_at` is older than the lease (e.g. `LOCKED`/`RUNNING` with
  `locked_at < now() - lease`) back to `READY`, clearing `lock_owner`/
  `locked_at`. This makes claims crash-safe without external coordination.
- **Backoff/defer:** set `state='RETRY_WAIT'`/`'DEFERRED'` (or `'POLL_WAIT'`)
  and `next_run_at = now() + backoff`; the row only becomes claimable again
  once it is `READY` and `next_run_at` has elapsed.

---

## 3. `remote_queries` — submitted query lifecycle (`QueryState`)

Enum: `QueryState`. Column: `remote_query.state`. A `remote_query` is one
submitted ClassyFire query/batch; `remote_query_items` map it to the
tasks/batch items/compounds it resolves.

### States

| State | Meaning | Terminal? |
| --- | --- | --- |
| `SUBMITTED` | Payload submitted upstream; `remote_query_id` recorded. | no |
| `POLLING` | Being polled for completion (`next_poll_at` schedules polls). | no |
| `COMPLETE` | Upstream returned results; `raw_response`/`completed_at` set. | yes |
| `FAILED` | Permanent upstream error / gave up. | yes |

### Allowed transitions

```
SUBMITTED ─▶ POLLING ─▶ COMPLETE        (results retrieved)
                    └─▶ FAILED          (permanent error / exhausted)
SUBMITTED ─▶ FAILED                     (immediate rejection)
```

`COMPLETE` and `FAILED` are terminal; `completed_at` is set on entry to either.

---

## 4. `batch_jobs` — job lifecycle (`BatchJobStatus`)

Enum: `BatchJobStatus` (in `apps/core/enums.py`). Column: `batch_jobs.status`.

A `BatchJob` is created when a batch upload is accepted. Its status coarsely
tracks whether processing is outstanding, complete, or has failed.

### States

| State | Meaning | Terminal? |
| --- | --- | --- |
| `PENDING` | Created; items not yet normalized or processed. | no |
| `IN_PROGRESS` | Normalization / lookup / remote work is ongoing. | no |
| `COMPLETE` | All items have reached a terminal `BatchItemStatus`. | yes |
| `FAILED` | Unrecoverable error; job cannot proceed. | yes |

### Allowed transitions

```
PENDING → IN_PROGRESS → COMPLETE   (all items resolved)
                      └→ FAILED    (unrecoverable error)
```

`COMPLETE` and `FAILED` are terminal. The `completed_at` timestamp is set on
entry to either terminal state.

---

## 5. `classification_results` — append-only immutable event log (F-05/F-20)

Enum: `ResultKind`. Column: `classification_results.kind`. Table:
`classification_results`.

`classification_results` is an **append-only, immutable** event log. EVERY write
to the classification store (`write_live_result`) appends exactly ONE row — one
`INSERT`, and the row is **never `UPDATE`d or `DELETE`d** by any code path (Python
today, a future Go/Rust writer that talks to the same Postgres schema tomorrow).
`classyfire_live.result` is a FK to the event that materialized the current live
row; superseded events stay in the table forever as the audit history.

### `kind` — disposition of an event relative to the live pointer at write time

| `kind` | Meaning | Live effect |
|---|---|---|
| `APPLIED` | Materialized the current live row (claimed an empty slot). | live created, `result` → this event |
| `SUPERSEDED` | Overwrote an existing live row (prior row archived to `classification_history` first). | live updated in place, `result` → this event |
| `REJECTED_MISMATCH` | Source returned a different InChIKey than requested. The mismatch is **STORED, not discarded**. | none (live unchanged) |
| `REJECTED_LOWER_PRIORITY` | A strictly higher-trust row already exists. | none |
| `REJECTED_STALE` | Same priority but the existing live row is newer-or-equal. | none |

`requested_inchikey` is the key we asked for; `returned_inchikey` (nullable) is
the key the source returned — they differ exactly on a `REJECTED_MISMATCH` event.
`raw_json` carries the upstream payload and `raw_checksum` is the sha256 hex of
its canonical JSON encoding (reproducible across languages). DB CHECK constraints
enforce the 27-char InChIKey lengths and the 64-char hex checksum.

`source_artifact` (FK → `source_artifacts`) links events emitted by the
provenance-preserving official-import path (`SourceType.LAB_OFFICIAL_IMPORT`):
stage → validate → record immutable `SourceArtifact` (sha256 + byte_size) → emit
immutable event(s) → advance the live pointer. `source_artifacts` is likewise
append-only by contract.

`batch_items.resolved_classification` (FK → `classification_results`) pins the
immutable event a resolved batch item used; exports read this pinned event (or the
pinned `classification_snapshot` JSON) and **never re-query live**, so an export is
byte-stable even after live is later superseded.

## 6. Other enums (no transition graph)

- `SourceType` + `SOURCE_PRIORITY` — provenance/trust of a `classyfire_live`
  row. Priority is stored as the integer column `classyfire_live.source_priority`
  so overwrite arbitration (`new.priority >= current.priority`) is portable.
  Exact map: `WISHART_LIVE_QUERY=100`, `WISHART_LIVE_ENTITY=100`,
  `ARCHIVED_WISHART_JSON=90`, `LAB_OFFICIAL_IMPORT=85`, `HISTORICAL_AGGREGATE=70`.
- `DetectedInputType` — detected shape of a raw input row
  (`INCHIKEY/INCHI/SMILES/SDF/UNKNOWN`).
- `SeedProfile` — `FULL/LITE/CUSTOM` seed shape selector.
- `EventCategory` — `WORKER/REMOTE/BACKUP/SEED/SECURITY` for `system_events`.
