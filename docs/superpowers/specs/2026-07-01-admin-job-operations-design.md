# Admin Job Operations Design

**Date:** 2026-07-01
**Status:** Review required

## Goal

Expand the Django admin from a mostly read-only inspection console into a
minimal operations console for batch jobs while preserving the core safety rule:
admins must not directly edit database rows through form fields.

The admin must support:

- opening a job's result page without recovering or exposing the original
  `result_secret`;
- deleting rows from the admin when the actor is a superuser;
- canceling and deleting an in-progress `BatchJob`;
- changing job processing priority through explicit audited actions, not raw
  field edits.

## Permission Model

Keep `add` and `change` disabled for the existing admin surface.

Allow `delete` for active superusers only. Non-superuser staff remain view-only.
This applies through a shared admin base so registered operational models get
consistent behavior.

Direct edits remain blocked because many rows carry invariants maintained by
services: append-only classification events, live pointers, remote queue leases,
export capability tokens, and audit records.

## Job Result Access

Add an admin-only view under `BatchJobAdmin`:

`/admin/batches/batchjob/<job_id>/open-result/`

The view must:

1. require the same superuser operation permission used by mutating actions;
2. set the normal job access cookie via `set_job_access_cookie`;
3. redirect to `/jobs/<job_id>/`;
4. write a `SystemEvent` with actor and job id.

The view must not show, regenerate, or persist a plaintext job secret.

Expose the link from `BatchJobAdmin.list_display` and the job detail readonly
fields.

## Delete Behavior

Enable the Django admin delete flow for superusers, but customize `BatchJob`
deletion so filesystem artifact paths are collected before the row disappears
and files are unlinked after the database delete succeeds.

For each deleted `BatchJob`:

- collect and unlink `upload_artifact_path` using `unlink_upload_artifact`;
- collect linked `ExportArtifact` rows and unlink their payload files and legacy
  `.meta.json` sidecars;
- allow database cascades to remove `BatchItem` and `ExportArtifact` rows;
- write one `SystemEvent` for the delete operation with actor, job ids, counts,
  and filesystem cleanup counts.

Other models can use the default admin delete flow, with a shared audit hook
that records model label, object ids, and actor.

## Cancel And Delete

Add a separate `BatchJobAdmin` action: `Cancel and delete selected jobs`.

This action is for jobs that may still be in progress. Database state changes
must run inside a transaction; filesystem paths must be collected before row
deletion and unlinked only after the database delete succeeds.

1. find selected-job `BatchItem` rows with an InChIKey;
2. identify active `RemoteTask` rows for those InChIKeys;
3. cancel an active `RemoteTask` only when it is unleased (`lock_owner IS NULL`)
   and no non-selected job still has a pending/delayed item for the same
   InChIKey;
4. skip leased or shared remote tasks and report both skipped counts;
5. delete the selected jobs through the same artifact-cleaning path used by
   normal job deletion;
6. write a `SystemEvent` with actor, job ids, canceled task count, skipped leased
   task count, skipped shared task count, deleted job count, and cleanup counts.

The action must not steal active remote leases or cancel work still needed by a
different job. If a worker is currently holding a task, the task is skipped and
the operator can pause remote processing first or retry the action after the
lease clears.

## Job Priority

Add `BatchJob.priority` as an integer column with default `0`. Higher values are
processed first.

Update ingest claiming order from:

`created_at ASC, id ASC`

to:

`priority DESC, created_at ASC, id ASC`

Add `BatchJobAdmin` actions:

- `Set high priority` sets selected jobs to `100`;
- `Reset priority` sets selected jobs to `0`.

When a job priority changes, recompute active `RemoteTask.priority` for affected
InChIKeys as the maximum `BatchJob.priority` among remaining pending/delayed
items waiting on that InChIKey. Leased tasks are left untouched and reported as
skipped. New remote tasks created by ingest must receive the owning job's
priority, and enqueueing against an existing active task must raise that task's
priority when the new waiting job has a higher priority.

Each action writes a `SystemEvent`.

## Safety And Tests

Add tests for:

- non-superuser staff cannot delete or use job operation views/actions;
- superuser can delete operational rows while add/change remain blocked;
- deleting a `BatchJob` removes upload artifacts and export payloads/sidecars;
- opening a result page sets a job access cookie and redirects without exposing
  the secret;
- cancel-and-delete deletes jobs, cancels only unleased active remote tasks, and
  audits skipped leased tasks;
- priority actions update `BatchJob.priority`, affect ingest claim order, update
  unleased remote task priority, and audit the operation.

Use the existing `tests/test_admin_console.py` style and focused service tests
for artifact cleanup and worker claim ordering.
