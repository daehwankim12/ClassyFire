# Admin Job Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe Django admin operations for job result access, row deletion, cancel-and-delete, and job priority while keeping direct row editing disabled.

**Architecture:** Add focused batch admin operation helpers in `apps/batches/admin_ops.py`; keep `ModelAdmin` classes thin and audited. Add `BatchJob.priority` with migration support, propagate job priority into remote tasks, and preserve `RemoteTask` sharing semantics by checking other waiting jobs before canceling a task.

**Tech Stack:** Django 5.2 admin, ORM transactions, pytest-django, existing `SystemEvent` audit model, existing remote queue and batch service models.

---

### Task 1: Delete Permission Contract

- [x] Write tests proving add/change stay blocked and delete is superuser-only.
- [x] Update `ReadOnlyModelAdmin.has_delete_permission` to delegate to `has_operate_permission`.
- [x] Verify focused admin permission tests or compare the DB setup failure to baseline.

### Task 2: Job Priority Model And Queue Propagation

- [x] Add `BatchJob.priority` to model and initial migration.
- [x] Change ingest claim ordering to `priority DESC, created_at ASC, id ASC`.
- [x] Pass job priority into remote task enqueueing and raise existing remote task priority without lowering it.
- [x] Add focused tests for claim ordering and enqueue priority upgrade.

### Task 3: Batch Admin Operation Helpers

- [x] Create `apps/batches/admin_ops.py`.
- [x] Add `delete_jobs_with_artifacts`, `cancel_and_delete_jobs`, `set_job_priority`, and remote priority recomputation helpers.
- [x] Add tests for artifact cleanup, shared remote task preservation, unleased remote task cancellation, and priority recomputation.

### Task 4: Admin Wiring

- [x] Add BatchJob open-result admin URL/link.
- [x] Add BatchJob actions for cancel-and-delete, high priority, and normal priority.
- [x] Add tests for admin action behavior and result-page redirect cookie.

### Task 5: Verification

- [x] Run `python manage.py check`.
- [x] Run `ruff check` for modified files.
- [x] Run focused tests; if DB host `db` is unavailable, record that the failure matches the captured baseline environment failure.
- [x] Commit the worktree branch.
