# Backup Bundle Export/Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add operator commands that package an existing backup manifest and its files into one portable `.tar.gz`, then import that bundle back into the backup store after checksum verification.

**Architecture:** Keep the existing backup and restore pipeline intact. Add a focused `apps.operations.backup_bundle` module that reads existing backup manifests, safely resolves referenced files, creates a bundle with a small bundle index, and imports bundles into a staging/backup directory without restoring the live DB by default.

**Tech Stack:** Django management commands, Python `tarfile`, SHA-256 checksums, existing `apps.operations.backup` helpers, pytest.

---

### Task 1: Bundle Core

**Files:**
- Create: `apps/operations/backup_bundle.py`
- Test: `tests/test_backup_bundle.py`

- [ ] **Step 1: Write failing tests**
  - Test exporting the latest manifest into one `.tar.gz`.
  - Test import extracts the files into the backup directory and verifies manifest checksums.
  - Test path traversal members are rejected.

- [ ] **Step 2: Run tests to verify RED**

Run:
```bash
uv run --frozen --extra dev python scripts/run_postgres_tests.py tests/test_backup_bundle.py -q
```

Expected: import error for `apps.operations.backup_bundle`.

- [ ] **Step 3: Implement core**
  - `create_backup_bundle(output_path, backup_dir=None, manifest_file=None, overwrite=False) -> dict`
  - `import_backup_bundle(bundle_path, backup_dir=None, overwrite=False) -> dict`
  - `verify_backup_manifest_files(backup_dir, manifest_name) -> dict`
  - Reject absolute paths, nested paths, missing files, checksum mismatches, and unsafe tar members.

- [ ] **Step 4: Run tests to verify GREEN**

Run:
```bash
uv run --frozen --extra dev python scripts/run_postgres_tests.py tests/test_backup_bundle.py -q
```

Expected: all tests pass.

### Task 2: Management Commands

**Files:**
- Create: `apps/operations/management/commands/export_backup_bundle.py`
- Create: `apps/operations/management/commands/import_backup_bundle.py`
- Test: `tests/test_backup_bundle.py`

- [ ] **Step 1: Write failing command tests**
  - `export_backup_bundle --backup-dir <dir> --output <file>` prints bundle path and checksum.
  - `import_backup_bundle <file> --backup-dir <dir>` prints imported manifest and restored command hint.

- [ ] **Step 2: Run tests to verify RED**

Run:
```bash
uv run --frozen --extra dev python scripts/run_postgres_tests.py tests/test_backup_bundle.py -q
```

Expected: unknown management command failures.

- [ ] **Step 3: Implement commands**
  - Wrap core exceptions in `CommandError`.
  - Keep import non-destructive: it stages files into the backup directory only.
  - Print the follow-up `restore.sh` hint rather than restoring DB.

- [ ] **Step 4: Run tests to verify GREEN**

Run:
```bash
uv run --frozen --extra dev python scripts/run_postgres_tests.py tests/test_backup_bundle.py -q
```

Expected: all tests pass.

### Task 3: Documentation and Verification

**Files:**
- Modify: `docs/operations.md`
- Test: targeted backup tests and static checks

- [ ] **Step 1: Document operator flow**
  - How to export bundle from `/backups`.
  - How to copy it out with `docker cp`.
  - How to import it back.
  - State explicitly that import is non-destructive and restore remains explicit.

- [ ] **Step 2: Verify**

Run:
```bash
uv run --frozen --extra dev python scripts/run_postgres_tests.py tests/test_backup_bundle.py tests/test_backup_artifact_manifest.py tests/test_operations.py::test_backup_creates_manifest_and_event tests/test_operations.py::test_backup_checksum_matches_file tests/test_operations.py::test_backup_excludes_seed_in_manifest -q
uv run --frozen --extra dev ruff check apps/operations/backup_bundle.py apps/operations/management/commands/export_backup_bundle.py apps/operations/management/commands/import_backup_bundle.py tests/test_backup_bundle.py
uv run --frozen --extra dev mypy apps/operations/backup_bundle.py apps/operations/management/commands/export_backup_bundle.py apps/operations/management/commands/import_backup_bundle.py
git diff --check
```

Expected: all pass.
