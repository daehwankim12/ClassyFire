# Negative Cache Manual Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop Wishart tail failures from monopolizing jobs by tracking per-InChIKey remote failures, allowing admin-only manual fallback data for hard failures, and rechecking Wishart later.

**Architecture:** Add a remote negative-cache model and a separate manual fallback model. Worker failure paths update negative-cache state; batch processing consults Wishart live cache first, then manual fallback only when Wishart has no live result. Admin actions can requeue rechecks and edit fallback rows only through the negative-cache surface.

**Tech Stack:** Django 5.2, PostgreSQL, pytest-django, existing remote worker and classification writer services.

---

### Task 1: Remote Negative Cache Model

**Files:**
- Modify: `apps/remote/models.py`
- Modify: `apps/remote/admin.py`
- Create: `apps/remote/migrations/0002_remote_negative_cache_manual_override.py`
- Test: `tests/test_remote_negative_cache.py`

- [ ] Step 1: Add failing tests for recording submit/poll failures and hard-fail threshold.
- [ ] Step 2: Run targeted test and verify it fails because the model/service is missing.
- [ ] Step 3: Add `RemoteNegativeCache`, `ManualClassificationOverride`, helper service methods, and migration.
- [ ] Step 4: Run targeted test and verify it passes.

### Task 2: Worker Failure Integration

**Files:**
- Modify: `apps/remote/worker.py`
- Modify: `apps/remote/services.py`
- Test: `tests/test_remote_negative_cache.py`
- Test: `tests/test_remote_entity_500_fallback.py`

- [ ] Step 1: Add failing tests for entity 500 count, submit escalation after three entity failures, and submit/poll failure recording.
- [ ] Step 2: Run targeted tests and verify they fail on missing behavior.
- [ ] Step 3: Update entity retry handling and submit/poll failure paths to record negative-cache candidates without bypassing existing audit attempts.
- [ ] Step 4: Run targeted tests and verify they pass.

### Task 3: Manual Fallback Lookup

**Files:**
- Modify: `apps/batches/services.py`
- Modify: `apps/batches/exports.py`
- Test: `tests/test_remote_negative_cache.py`
- Test: `tests/test_exports.py`

- [ ] Step 1: Add failing tests showing hard-negative items use active manual fallback only when no Wishart live cache exists.
- [ ] Step 2: Run targeted tests and verify they fail on missing fallback.
- [ ] Step 3: Add manual fallback lookup and export source labeling without inserting fallback rows into `ClassyfireLive`.
- [ ] Step 4: Run targeted tests and verify they pass.

### Task 4: Admin Operations

**Files:**
- Modify: `apps/remote/admin.py`
- Modify: `apps/remote/services.py`
- Test: `tests/test_admin_console.py`
- Test: `tests/test_remote_negative_cache.py`

- [ ] Step 1: Add failing tests for admin-only manual fallback creation/editing, delete, and Wishart recheck action.
- [ ] Step 2: Run targeted tests and verify they fail on missing admin actions.
- [ ] Step 3: Add negative-cache admin surface, constrained manual fallback edit form, and recheck/reset actions.
- [ ] Step 4: Run targeted tests and verify they pass.

### Task 5: Verification

**Files:**
- All modified files.

- [ ] Run targeted pytest files.
- [ ] Run `ruff check` on modified Python files.
- [ ] Run `python -m compileall` on modified app modules.
- [ ] Run `python manage.py check`.
- [ ] Commit implementation.
