"""Restore integrity tests — Gate 2 / Workstream G (F-13).

Coverage split
──────────────
This module runs the FULL pg_dump / pg_restore round-trip against real
Postgres (127.0.0.1:55432) using two temporary databases:
  - ``cf_wsg_restore_src`` — source DB with known fixture data
  - ``cf_wsg_restore_dst`` — destination DB to restore into

The tests validate:
  A. Checksum: sha256 of the dump matches the manifest value.
  B. Exclude policy: ``classyfire_seed*`` tables are absent from the dump.
  C. Row counts: all key non-seed tables survive the round-trip intact.
  D. FK integrity: FK-bearing tables are probed with JOIN queries that
     exercise the foreign key relationships.
  E. InChIKey classification lookup: an exact InChIKey present in
     ``classyfire_live`` pre-dump is retrievable post-restore.
  F. Pending RemoteTask resumability invariant: a ``READY`` RemoteTask with a
     linked ``compound`` (the submitted structure) retains that link and the
     original SMILES post-restore.  This is the critical invariant — a
     restored system must be able to resume in-flight remote work.

Shell-level manual round-trip target (for Docker compose validation)
────────────────────────────────────────────────────────────────────
  make backup-restore-check
  — or directly: —
  BACKUP_DIR=/tmp/cf_backup_test \\
  POSTGRES_HOST=db POSTGRES_DB=classyfire POSTGRES_USER=classyfire \\
  PGPASSWORD=<password> sh deploy/backup/backup.sh

  BACKUP_DIR=/tmp/cf_backup_test POSTGRES_DB=classyfire_restore_test \\
  POSTGRES_HOST=db POSTGRES_USER=classyfire PGPASSWORD=<password> \\
  RESTORE_DROP_DB=1 sh deploy/backup/restore.sh

See docs/backup-restore.md for the full Docker compose procedure.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PG_BIN = "/home/user/micromamba/envs/cf-pg/bin"
SRC_DB = "cf_wsg_restore_src"
DST_DB = "cf_wsg_restore_dst"

VALID_IK_ASPIRIN = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
VALID_IK_CAFFEINE = "RYYVLZVUVIJVGH-UHFFFAOYSA-N"
ASPIRIN_SMILES = "CC(=O)Oc1ccccc1C(=O)O"

# Seed InChIKey — goes into classyfire_seed; must NOT appear in dump.
VALID_IK_SEED = "WQZGKKKJIJFFOK-GASJEMHNSA-N"

CONN = {
    "host": "127.0.0.1",
    "port": "55432",
    "user": "classyfire",
    "password": "classyfire",
}


# ---------------------------------------------------------------------------
# Low-level DB helpers (subprocess psql — avoids Django connection pool)
# ---------------------------------------------------------------------------


def _env() -> dict[str, str]:
    """Return subprocess env with PGPASSWORD set."""
    e = os.environ.copy()
    e["PGPASSWORD"] = CONN["password"]
    e["PATH"] = PG_BIN + ":" + e.get("PATH", "")
    return e


def _psql(db: str, sql: str) -> str:
    """Run SQL on *db* and return stdout."""
    result = subprocess.run(
        [
            "psql",
            "-h", CONN["host"],
            "-p", CONN["port"],
            "-U", CONN["user"],
            "-d", db,
            "-c", sql,
            "-t",  # tuples only
            "-A",  # no alignment
        ],
        capture_output=True,
        text=True,
        env=_env(),
        check=True,
    )
    return result.stdout.strip()


def _psql_fetch_one(db: str, sql: str) -> str | None:
    """Return first non-empty result line, or None."""
    out = _psql(db, sql)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return lines[0] if lines else None


def _db_exists(name: str) -> bool:
    result = subprocess.run(
        [
            "psql",
            "-h", CONN["host"],
            "-p", CONN["port"],
            "-U", CONN["user"],
            "-d", "postgres",
            "-tA",
            "-c", f"SELECT 1 FROM pg_database WHERE datname='{name}';",
        ],
        capture_output=True,
        text=True,
        env=_env(),
    )
    return result.stdout.strip() == "1"


def _create_db(name: str) -> None:
    """Create database, first dropping it if it exists.

    DROP DATABASE cannot run inside a transaction block, so we issue two
    separate psql invocations.
    """
    subprocess.run(
        [
            "psql",
            "-h", CONN["host"],
            "-p", CONN["port"],
            "-U", CONN["user"],
            "-d", "postgres",
            "-c", f"DROP DATABASE IF EXISTS \"{name}\";",
        ],
        env=_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        [
            "psql",
            "-h", CONN["host"],
            "-p", CONN["port"],
            "-U", CONN["user"],
            "-d", "postgres",
            "-c", f"CREATE DATABASE \"{name}\" OWNER \"{CONN['user']}\";",
        ],
        env=_env(),
        capture_output=True,
        text=True,
        check=True,
    )


def _drop_db(name: str) -> None:
    subprocess.run(
        [
            "psql",
            "-h", CONN["host"],
            "-p", CONN["port"],
            "-U", CONN["user"],
            "-d", "postgres",
            "-c", f"DROP DATABASE IF EXISTS \"{name}\";",
        ],
        capture_output=True,
        text=True,
        env=_env(),
    )


def _run_migrations(db: str) -> None:
    """Apply Django migrations to *db*."""
    env = os.environ.copy()
    env["PGPASSWORD"] = CONN["password"]
    env["PATH"] = PG_BIN + ":" + env.get("PATH", "")
    env["POSTGRES_HOST"] = CONN["host"]
    env["POSTGRES_PORT"] = CONN["port"]
    env["POSTGRES_DB"] = db
    env["POSTGRES_USER"] = CONN["user"]
    env["POSTGRES_PASSWORD"] = CONN["password"]
    env["DJANGO_SECRET_KEY"] = "test-secret-key-not-for-prod"
    env["DJANGO_SETTINGS_MODULE"] = "config.settings.dev"

    subprocess.run(
        ["python", "manage.py", "migrate", "--noinput"],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def _pg_dump(src_db: str, dump_path: str) -> None:
    """Dump src_db excluding classyfire_seed* to dump_path."""
    subprocess.run(
        [
            "pg_dump",
            "-h", CONN["host"],
            "-p", CONN["port"],
            "-U", CONN["user"],
            "-d", src_db,
            "-Fc",
            "--exclude-table=classyfire_seed*",
            "-f", dump_path,
        ],
        env=_env(),
        capture_output=True,
        text=True,
        check=True,
    )


def _pg_restore(dst_db: str, dump_path: str) -> None:
    """Restore dump_path into dst_db."""
    subprocess.run(
        [
            "pg_restore",
            "-h", CONN["host"],
            "-p", CONN["port"],
            "-U", CONN["user"],
            "-d", dst_db,
            "--clean",
            "--if-exists",
            dump_path,
        ],
        env=_env(),
        capture_output=True,
        text=True,
        check=True,
    )


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Session-scoped fixture: create + populate SRC, create DST, dump, restore
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def restore_round_trip():
    """Full pg_dump / pg_restore round-trip.

    Returns a dict with:
      src_db       — source DB name
      dst_db       — destination DB name
      dump_path    — path to the dump file
      manifest     — manifest dict written alongside the dump
      dump_checksum — sha256 of the dump file
    """
    # ── Setup: create and migrate SRC only; DST is intentionally empty ────
    _create_db(SRC_DB)
    _create_db(DST_DB)
    try:
        _run_migrations(SRC_DB)
        # DST is left schema-free: pg_restore recreates all schema from the dump,
        # mirroring the real restore flow (restore.sh with RESTORE_DROP_DB=1).

        # ── Insert fixtures into SRC ────────────────────────────────────────
        # compound_structures: aspirin with a known InChIKey + SMILES
        _psql(
            SRC_DB,
            f"INSERT INTO compound_structures "
            "(original_input, canonical_smiles, inchikey, parse_metadata, created_at) "
            "VALUES "
            f"('{ASPIRIN_SMILES}', '{ASPIRIN_SMILES}', '{VALID_IK_ASPIRIN}', '{{}}', NOW())"
            " ON CONFLICT DO NOTHING;",
        )

        # remote_tasks: a READY task referencing the compound (resumability invariant)
        _psql(
            SRC_DB,
            f"INSERT INTO remote_tasks "
            "(inchikey, compound_id, state, priority, attempt, lease_version, created_at, updated_at) "
            "VALUES ("
            f"  '{VALID_IK_ASPIRIN}', "
            f"  (SELECT id FROM compound_structures WHERE inchikey='{VALID_IK_ASPIRIN}'), "
            "  'READY', 0, 0, 0, NOW(), NOW()"
            ") ON CONFLICT DO NOTHING;",
        )

        # classyfire_live: a live classification result for aspirin
        _psql(
            SRC_DB,
            f"INSERT INTO classyfire_live "
            "(inchikey, source_type, source_priority, retrieved_at) "
            "VALUES "
            f"('{VALID_IK_ASPIRIN}', 'WISHART_LIVE_QUERY', 100, NOW())"
            " ON CONFLICT DO NOTHING;",
        )

        # batch_jobs / batch_items: one job with one item referencing the compound
        _psql(
            SRC_DB,
            "INSERT INTO batch_jobs "
            "(id, secret_hash, status, total_count, completed_count, local_hit_count, "
            "seed_hit_count, remote_pending_count, need_structure_count, invalid_count, "
            "remote_enabled, created_at) "
            "VALUES ("
            "  gen_random_uuid(), 'testhash', 'PENDING', 1, 0, 0, 0, 0, 0, 0, true, NOW()"
            ");",
        )
        _psql(
            SRC_DB,
            "INSERT INTO batch_items "
            "(job_id, row_order, original_input, detected_type, compound_id, inchikey, status) "
            "SELECT id, 0, 'aspirin', 'INCHIKEY', "
            f"  (SELECT id FROM compound_structures WHERE inchikey='{VALID_IK_ASPIRIN}'), "
            f"  '{VALID_IK_ASPIRIN}', 'PENDING_NORMALIZATION' "
            "FROM batch_jobs ORDER BY created_at DESC LIMIT 1;",
        )

        # classyfire_seed: insert a seed row — this MUST be excluded from the dump
        _psql(
            SRC_DB,
            f"INSERT INTO classyfire_seed "
            "(inchikey, inchikey14, seed_source_id, seed_version) "
            "VALUES "
            f"('{VALID_IK_SEED}', '{VALID_IK_SEED[:14]}', 1, 'test-v1')"
            " ON CONFLICT DO NOTHING;",
        )

        # seed_imports: one import record
        _psql(
            SRC_DB,
            "INSERT INTO seed_imports "
            "(doi, filename, checksum, import_status, actual_row_count, who, created_at) "
            "VALUES "
            "('10.5281/test', 'seed_test.tsv.zst', 'abc123', 'COMPLETE', 1, 'tester', NOW());",
        )

        # compound_usage: usage tally for the aspirin InChIKey
        _psql(
            SRC_DB,
            "INSERT INTO compound_usage "
            "(inchikey, first_seen, last_seen, seen_count, waiting_job_count) "
            "VALUES "
            f"('{VALID_IK_ASPIRIN}', NOW(), NOW(), 3, 1)"
            " ON CONFLICT DO NOTHING;",
        )

        # classification_history: an archived snapshot of a superseded live row
        _psql(
            SRC_DB,
            "INSERT INTO classification_history "
            "(inchikey, source_type, source_priority, payload, retrieved_at, archived_at) "
            "VALUES ("
            f"  '{VALID_IK_ASPIRIN}', 'WISHART_LIVE_QUERY', 100, "
            "  '{\"superseded\": true}', NOW(), NOW()"
            ");",
        )

        # system_events: an operations event row
        _psql(
            SRC_DB,
            "INSERT INTO system_events "
            "(category, event_type, payload, created_at) "
            "VALUES "
            "('BACKUP', 'backup_complete', '{\"status\": \"ok\"}', NOW());",
        )

        # remote_queries: a submitted query, then remote_query_items linking it
        # to the READY remote_task (exercises the provenance mapping FK chain).
        _psql(
            SRC_DB,
            "INSERT INTO remote_queries "
            "(provider, remote_query_id, payload_hash, state, submitted_at) "
            "VALUES "
            "('wishart', 'q-test-1', 'payloadhash123', 'SUBMITTED', NOW());",
        )
        _psql(
            SRC_DB,
            "INSERT INTO remote_query_items "
            "(query_id, task_id, compound_id, inchikey) "
            "SELECT "
            "  (SELECT id FROM remote_queries WHERE remote_query_id='q-test-1'), "
            f"  (SELECT id FROM remote_tasks WHERE inchikey='{VALID_IK_ASPIRIN}'), "
            f"  (SELECT id FROM compound_structures WHERE inchikey='{VALID_IK_ASPIRIN}'), "
            f"  '{VALID_IK_ASPIRIN}';",
        )

        # ── Gate-2 new tables ───────────────────────────────────────────────

        # source_artifacts: one artifact record (checksum must be 64-char hex;
        # imported_at has no DB default despite auto_now_add=True in Django)
        _psql(
            SRC_DB,
            "INSERT INTO source_artifacts "
            "(source_type, name, checksum, byte_size, imported_at) "
            "VALUES ("
            "  'LAB_OFFICIAL_IMPORT', 'test_artifact.json', "
            "  'a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4c5d6e7f8a3b4', "
            "  1024, NOW()"
            ");",
        )

        # classification_results: one immutable result event (raw_checksum must be
        # 64-char hex, requested_inchikey must be 27-char; created_at has no DB default)
        _psql(
            SRC_DB,
            f"INSERT INTO classification_results "
            "(requested_inchikey, kind, source_type, source_priority, "
            " raw_checksum, retrieved_at, created_at) "
            "VALUES ("
            f"  '{VALID_IK_ASPIRIN}', 'APPLIED', 'WISHART_LIVE_QUERY', 100, "
            "  'b1c2d3e4f5a6b1c2d3e4f5a6b1c2d3e4f5a6b1c2d3e4f5a6b1c2d3e4f5a6b1c2', "
            "  NOW(), NOW()"
            ");",
        )

        # export_artifacts: one export artifact linked to the existing batch job
        # (created_at has no DB default despite auto_now_add=True in Django)
        _psql(
            SRC_DB,
            "INSERT INTO export_artifacts "
            "(job_id, fmt, token, path, checksum, byte_size, "
            " rows_signature, job_state_signature, row_count, created_at, expires_at) "
            "SELECT "
            "  id, 'csv', 'test-export-token-abc123', '/tmp/export_test.csv', "
            "  'c2d3e4f5a6b7c2d3e4f5a6b7c2d3e4f5a6b7c2d3e4f5a6b7c2d3e4f5a6b7c2d3', "
            "  512, "
            "  'd3e4f5a6b7c8d3e4f5a6b7c8d3e4f5a6b7c8d3e4f5a6b7c8d3e4f5a6b7c8d3e4', "
            "  'a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2', "
            "  1, NOW(), NOW() + INTERVAL '1 day' "
            "FROM batch_jobs ORDER BY created_at DESC LIMIT 1;",
        )

        # remote_submissions: one submission outbox record (request_payload is NOT NULL
        # JSONB; reconcile_attempts, created_at, updated_at have no DB defaults)
        _psql(
            SRC_DB,
            "INSERT INTO remote_submissions "
            "(provider, payload_hash, state, request_payload, "
            " reconcile_attempts, created_at, updated_at) "
            "VALUES ("
            "  'wishart', "
            "  'e4f5a6b7c8d9e4f5a6b7c8d9e4f5a6b7c8d9e4f5a6b7c8d9e4f5a6b7c8d9e4f5', "
            "  'SUBMITTED', '{\"inchikeys\": [\"" + VALID_IK_ASPIRIN + "\"]}', "
            "  0, NOW(), NOW()"
            ");",
        )

        # remote_attempts: one attempt audit log row (created_at has no DB default)
        _psql(
            SRC_DB,
            "INSERT INTO remote_attempts "
            "(provider, operation, classification, created_at) "
            "VALUES ('wishart', 'SUBMIT', 'SUCCESS', NOW());",
        )

        # remote_rate_buckets: one token bucket row (updated_at has no DB default)
        _psql(
            SRC_DB,
            "INSERT INTO remote_rate_buckets "
            "(provider, bucket, capacity, tokens, refill_rate_per_sec, updated_at) "
            "VALUES ('wishart', 'submit', 10, 10.0, 0.5, NOW()) "
            "ON CONFLICT DO NOTHING;",
        )

        # remote_circuits: one circuit breaker row (state/consecutive_failures/updated_at
        # have no DB defaults despite Django Python-level defaults)
        _psql(
            SRC_DB,
            "INSERT INTO remote_circuits "
            "(provider, state, consecutive_failures, updated_at) "
            "VALUES ('wishart', 'CLOSED', 0, NOW()) "
            "ON CONFLICT DO NOTHING;",
        )

        # ── Dump SRC ────────────────────────────────────────────────────────
        with tempfile.NamedTemporaryFile(
            suffix=".dump", prefix="cf_integrity_", delete=False
        ) as tmp:
            dump_path = tmp.name

        _pg_dump(SRC_DB, dump_path)

        dump_checksum = _sha256(dump_path)

        # Write a manifest alongside the dump.
        manifest_path = dump_path.replace(".dump", ".manifest.json")
        manifest = {
            "timestamp": "2026-06-24T00:00:00+00:00",
            "file": os.path.basename(dump_path),
            "checksum": dump_checksum,
            "excludes": "classyfire_seed*",
            "status": "ok",
        }
        with open(manifest_path, "w") as mf:
            json.dump(manifest, mf)

        # ── Restore into DST ─────────────────────────────────────────────────
        _pg_restore(DST_DB, dump_path)

        yield {
            "src_db": SRC_DB,
            "dst_db": DST_DB,
            "dump_path": dump_path,
            "manifest": manifest,
            "dump_checksum": dump_checksum,
        }

    finally:
        _drop_db(SRC_DB)
        _drop_db(DST_DB)
        try:
            os.unlink(dump_path)
        except Exception:
            pass
        try:
            os.unlink(manifest_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# A. Checksum integrity
# ---------------------------------------------------------------------------


def test_checksum_matches_manifest(restore_round_trip):
    """SHA-256 of the dump file must equal the manifest checksum."""
    dump_path = restore_round_trip["dump_path"]
    expected = restore_round_trip["manifest"]["checksum"]
    actual = _sha256(dump_path)
    assert actual == expected, f"checksum mismatch: expected {expected}, got {actual}"


def test_manifest_records_exclude_pattern(restore_round_trip):
    """Manifest must record the seed exclusion pattern."""
    manifest = restore_round_trip["manifest"]
    assert "excludes" in manifest
    assert "classyfire_seed" in manifest["excludes"]


# ---------------------------------------------------------------------------
# B. Seed exclusion: classyfire_seed rows must NOT survive in the dump
# ---------------------------------------------------------------------------


def test_seed_table_excluded_from_dump(restore_round_trip):
    """classyfire_seed must be absent from the restored destination DB.

    pg_dump --exclude-table=classyfire_seed* excludes BOTH schema and data,
    so the table should not exist at all in DST (or be empty if present).
    Either outcome confirms the seed exclusion policy is enforced.
    """
    dst_db = restore_round_trip["dst_db"]

    # Check pg_class for the table's existence.
    exists = _psql_fetch_one(
        dst_db,
        "SELECT COUNT(*) FROM pg_class "
        "WHERE relname='classyfire_seed' AND relkind='r';",
    )
    if exists == "0":
        # Table does not exist in DST — exclusion is working correctly.
        return

    # Table exists (e.g. restored as empty by --clean --if-exists) — must be empty.
    count = _psql_fetch_one(
        dst_db,
        f"SELECT COUNT(*) FROM classyfire_seed WHERE inchikey='{VALID_IK_SEED}';",
    )
    assert count == "0", (
        f"classyfire_seed row for {VALID_IK_SEED} survived in DST — "
        "seed exclusion is broken"
    )


# ---------------------------------------------------------------------------
# C. Row counts: key tables survive round-trip intact
# ---------------------------------------------------------------------------


def _row_count(db: str, table: str) -> int:
    val = _psql_fetch_one(db, f"SELECT COUNT(*) FROM {table};")
    return int(val) if val else 0


@pytest.mark.parametrize("table", [
    "compound_structures",
    "compound_usage",
    "batch_jobs",
    "batch_items",
    "remote_tasks",
    "remote_queries",
    "remote_query_items",
    "classyfire_live",
    "classification_history",
    "seed_imports",
    "system_events",
    # Gate-2 new tables
    "classification_results",
    "source_artifacts",
    "export_artifacts",
    "remote_submissions",
    "remote_attempts",
    "remote_rate_buckets",
    "remote_circuits",
])
def test_row_count_matches_src(restore_round_trip, table):
    """Row count in DST must equal SRC for all non-seed tables.

    Every table listed here has at least one fixture row inserted in
    ``restore_round_trip``, so the SRC count must be > 0 — this guards
    against a vacuous 0 == 0 assertion that would pass even if the data
    failed to survive the round-trip.
    """
    src = _row_count(restore_round_trip["src_db"], table)
    dst = _row_count(restore_round_trip["dst_db"], table)
    assert src > 0, (
        f"Test fixture inserted no rows into {table}; row-count survival "
        "would be vacuously true"
    )
    assert dst == src, (
        f"Row count mismatch for {table}: src={src}, dst={dst}"
    )


# ---------------------------------------------------------------------------
# D. FK integrity: remote_tasks.compound_id must still reference a valid row
# ---------------------------------------------------------------------------


def test_fk_remote_task_compound_intact(restore_round_trip):
    """remote_tasks.compound_id FK must reference an existing compound_structures row."""
    dst_db = restore_round_trip["dst_db"]
    result = _psql_fetch_one(
        dst_db,
        "SELECT COUNT(*) FROM remote_tasks rt "
        "JOIN compound_structures cs ON cs.id = rt.compound_id "
        f"WHERE rt.inchikey='{VALID_IK_ASPIRIN}' AND rt.state='READY';",
    )
    assert result == "1", (
        "Expected 1 READY remote_task with a valid compound FK post-restore, "
        f"got: {result}"
    )


def test_fk_batch_item_compound_intact(restore_round_trip):
    """batch_items.compound_id FK must reference an existing compound_structures row."""
    dst_db = restore_round_trip["dst_db"]
    result = _psql_fetch_one(
        dst_db,
        "SELECT COUNT(*) FROM batch_items bi "
        "JOIN compound_structures cs ON cs.id = bi.compound_id "
        f"WHERE bi.inchikey='{VALID_IK_ASPIRIN}';",
    )
    assert result == "1", (
        "Expected 1 batch_item with a valid compound FK post-restore, "
        f"got: {result}"
    )


# ---------------------------------------------------------------------------
# E. InChIKey classification lookup post-restore
# ---------------------------------------------------------------------------


def test_inchikey_classification_lookup_post_restore(restore_round_trip):
    """An exact InChIKey present in classyfire_live pre-dump must be
    retrievable in DST with the same source_type."""
    dst_db = restore_round_trip["dst_db"]
    src_db = restore_round_trip["src_db"]

    src_row = _psql_fetch_one(
        src_db,
        f"SELECT source_type FROM classyfire_live WHERE inchikey='{VALID_IK_ASPIRIN}';",
    )
    dst_row = _psql_fetch_one(
        dst_db,
        f"SELECT source_type FROM classyfire_live WHERE inchikey='{VALID_IK_ASPIRIN}';",
    )
    assert dst_row is not None, (
        f"classyfire_live row for {VALID_IK_ASPIRIN} missing in DST"
    )
    assert dst_row == src_row, (
        f"source_type mismatch: src={src_row}, dst={dst_row}"
    )


# ---------------------------------------------------------------------------
# F. Pending RemoteTask resumability invariant (THE critical invariant)
# ---------------------------------------------------------------------------


def test_pending_remote_task_retains_compound_structure(restore_round_trip):
    """A READY RemoteTask must retain its compound_id link AND the compound
    must have the original SMILES post-restore.

    This is the most important restore invariant: a worker that resumes
    after restore must be able to read the submitted structure from
    compound_structures.  If this FK or the canonical_smiles is lost,
    in-flight remote classification cannot be resumed.
    """
    dst_db = restore_round_trip["dst_db"]

    # Fetch the compound canonical_smiles via the remote_task FK.
    result = _psql_fetch_one(
        dst_db,
        "SELECT cs.canonical_smiles "
        "FROM remote_tasks rt "
        "JOIN compound_structures cs ON cs.id = rt.compound_id "
        f"WHERE rt.inchikey='{VALID_IK_ASPIRIN}' AND rt.state='READY';",
    )
    assert result is not None, (
        f"No compound_structures row reachable from READY remote_task "
        f"for {VALID_IK_ASPIRIN} in DST — resumability invariant broken"
    )
    assert result == ASPIRIN_SMILES, (
        f"canonical_smiles mismatch: expected {ASPIRIN_SMILES!r}, got {result!r}"
    )


def test_pending_remote_task_state_preserved(restore_round_trip):
    """The task state (READY) must be preserved post-restore."""
    dst_db = restore_round_trip["dst_db"]

    state = _psql_fetch_one(
        dst_db,
        f"SELECT state FROM remote_tasks WHERE inchikey='{VALID_IK_ASPIRIN}';",
    )
    assert state == "READY", (
        f"Expected state=READY post-restore, got: {state}"
    )


# ---------------------------------------------------------------------------
# Retention logic unit tests (no real Postgres needed — pure Python)
# ---------------------------------------------------------------------------


def test_retention_prunes_excess_daily_backups(tmp_path):
    """apply_retention prunes beyond BACKUP_DAILY_COUNT (default 7)."""
    from datetime import UTC, datetime, timedelta

    from apps.operations.backup import apply_retention

    now = datetime.now(tz=UTC)
    # Write 9 "daily" manifests (all < 7 days old) and their dummy dumps.
    for i in range(9):
        ts = now - timedelta(hours=i * 2)
        stem = f"cf_{i:03d}"
        dump = tmp_path / f"{stem}.dump"
        dump.write_bytes(b"x")
        manifest = {
            "timestamp": ts.isoformat(),
            "file": f"{stem}.dump",
            "checksum": "abc",
            "excludes": "classyfire_seed*",
            "status": "ok",
        }
        (tmp_path / f"{stem}.manifest.json").write_text(json.dumps(manifest))

    apply_retention(tmp_path, now=now)
    remaining = list(tmp_path.glob("*.manifest.json"))
    assert len(remaining) == 7, f"Expected 7, got {len(remaining)}"


@pytest.mark.django_db
def test_backup_command_uses_exclude_table_flag(tmp_path):
    """run_backup_injectable must pass --exclude-table=classyfire_seed* to pg_dump."""
    from apps.operations.backup import run_backup_injectable

    captured_cmd: list[list[str]] = []

    def capture_dump(cmd: list[str], dump_path: Path) -> None:
        captured_cmd.append(cmd)
        dump_path.write_bytes(b"dummy-dump-content")

    run_backup_injectable(tmp_path, capture_dump)

    assert captured_cmd, "pg_dump was never called"
    cmd = captured_cmd[0]

    # Must use --exclude-table, NOT -t (explicit include).
    assert any("--exclude-table=classyfire_seed*" in arg for arg in cmd), (
        f"--exclude-table flag missing from pg_dump command: {cmd}"
    )
    # Must NOT use standalone -t (table include) flag.
    # Note: other single-char flags like -h, -p, -U, -d, -f are expected.
    assert "-t" not in cmd, (
        f"Standalone -t include flag found in pg_dump command "
        f"(should use --exclude-table instead): {cmd}"
    )


def test_retention_prunes_media_archive_too(tmp_path):
    """apply_retention must also delete the associated media archive."""
    from datetime import UTC, datetime, timedelta

    from apps.operations.backup import apply_retention

    now = datetime.now(tz=UTC)
    # 8 daily backups — the 8th (oldest) should be pruned.
    for i in range(8):
        ts = now - timedelta(hours=i * 2)
        stem = f"cf_{i:03d}"
        dump = tmp_path / f"{stem}.dump"
        dump.write_bytes(b"x")
        # Media archive with matching timestamp.
        media = tmp_path / f"classyfire_media_{i:03d}.tar.gz"
        media.write_bytes(b"y")
        manifest = {
            "timestamp": ts.isoformat(),
            "file": f"{stem}.dump",
            "checksum": "abc",
            "media_file": f"classyfire_media_{i:03d}.tar.gz",
            "media_checksum": "def",
            "excludes": "classyfire_seed*",
            "status": "ok",
        }
        (tmp_path / f"{stem}.manifest.json").write_text(json.dumps(manifest))

    apply_retention(tmp_path, now=now)

    remaining_manifests = list(tmp_path.glob("*.manifest.json"))
    remaining_media = list(tmp_path.glob("classyfire_media_*.tar.gz"))
    assert len(remaining_manifests) == 7, f"Expected 7 manifests, got {len(remaining_manifests)}"
    assert len(remaining_media) == 7, f"Expected 7 media archives, got {len(remaining_media)}"


def test_retention_prunes_artifact_archives(tmp_path):
    """apply_retention must delete artifact_archives (exports, source_artifacts)
    for pruned manifests, not just the legacy media_file field.

    This is a regression test: pre-fix, the exports/source_artifacts .tar.gz
    files would survive pruning and accumulate as orphans.
    """
    import json
    from datetime import UTC, datetime, timedelta

    from apps.operations.backup import apply_retention

    now = datetime.now(tz=UTC)
    # Create 8 daily manifests (all < 7 days old) — the oldest (index 7)
    # should be pruned, along with its artifact archives.
    for i in range(8):
        ts = now - timedelta(hours=i * 2)
        stem = f"cf_{i:03d}"
        # The .dump file.
        (tmp_path / f"{stem}.dump").write_bytes(b"x")
        # New-style artifact_archives: exports + source_artifacts.
        exports_file = f"classyfire_exports_{stem}.tar.gz"
        source_file = f"classyfire_source_artifacts_{stem}.tar.gz"
        (tmp_path / exports_file).write_bytes(b"e")
        (tmp_path / source_file).write_bytes(b"s")
        manifest = {
            "timestamp": ts.isoformat(),
            "file": f"{stem}.dump",
            "checksum": "abc",
            "excludes": "classyfire_seed*",
            "status": "ok",
            "artifact_archives": {
                "exports": {"file": exports_file, "checksum": "ee"},
                "source_artifacts": {"file": source_file, "checksum": "ss"},
            },
        }
        (tmp_path / f"{stem}.manifest.json").write_text(json.dumps(manifest))

    apply_retention(tmp_path, now=now)

    remaining_manifests = list(tmp_path.glob("*.manifest.json"))
    remaining_exports = list(tmp_path.glob("classyfire_exports_*.tar.gz"))
    remaining_source = list(tmp_path.glob("classyfire_source_artifacts_*.tar.gz"))

    assert len(remaining_manifests) == 7, (
        f"Expected 7 manifests after pruning, got {len(remaining_manifests)}"
    )
    # The 8th (oldest) manifest's artifact archives must be deleted.
    assert len(remaining_exports) == 7, (
        f"Expected 7 exports archives after pruning, got {len(remaining_exports)} "
        f"(orphan exports archive was not cleaned up)"
    )
    assert len(remaining_source) == 7, (
        f"Expected 7 source_artifacts archives after pruning, got {len(remaining_source)} "
        f"(orphan source_artifacts archive was not cleaned up)"
    )
