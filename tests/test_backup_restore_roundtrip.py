"""Regression test: restore.sh correctly parses artifact_archives from the manifest.

C1 regression — gate6-fixes
────────────────────────────
backup.sh writes artifact_archives entries as a SINGLE line per namespace:

    "exports": {"file": "classyfire_exports_20260625_010203.tar.gz", "checksum": "abc"}

With ``awk -F'"' '/"exports"/{found=1} found && /"file"/{print $4; exit}'``,
field $4 (with double-quote as separator) is the string ``file`` (the key),
not the filename (which is $6).  So the original restore.sh extracted the
literal string ``file`` into EXPORTS_FILE, the archive was never found, and
the restore silently skipped ALL export artifacts.

This test:
1. Constructs a manifest + tar archives in the EXACT format backup.sh produces.
2. Runs restore.sh with RESTORE_EXPORT_DIR and RESTORE_SOURCE_ARTIFACT_DIR set.
3. Asserts the known test files are extracted AND the checksum verification
   passed (not skipped).

The test is designed so it FAILS against the original buggy awk and PASSES
after the fix (``for(i=1;i<=NF;i++) if($i=="file"){print $(i+2); exit}``).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tarfile
import tempfile
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RESTORE_SH = Path(__file__).resolve().parent.parent / "deploy" / "backup" / "restore.sh"
PG_BIN = "/home/user/micromamba/envs/cf-pg/bin"

CONN = {
    "host": "127.0.0.1",
    "port": "55432",
    "user": "classyfire",
    "password": "classyfire",
}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _make_tar_gz(archive_path: Path, source_dir: Path) -> None:
    """Create a .tar.gz of source_dir contents (same as backup.sh's tar -czf)."""
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(source_dir, arcname=source_dir.name)


def _pg_env() -> dict[str, str]:
    """Subprocess env with pg binaries on PATH and PGPASSWORD set."""
    e = os.environ.copy()
    e["PGPASSWORD"] = CONN["password"]
    e["PATH"] = PG_BIN + ":" + e.get("PATH", "")
    return e


def _create_db(name: str) -> None:
    """Create a temporary database for the test dump."""
    env = _pg_env()
    subprocess.run(
        ["psql", "-h", CONN["host"], "-p", CONN["port"], "-U", CONN["user"],
         "-d", "postgres", "-c", f"DROP DATABASE IF EXISTS \"{name}\";"],
        env=env, capture_output=True, check=True,
    )
    subprocess.run(
        ["psql", "-h", CONN["host"], "-p", CONN["port"], "-U", CONN["user"],
         "-d", "postgres", "-c",
         f"CREATE DATABASE \"{name}\" OWNER \"{CONN['user']}\";"],
        env=env, capture_output=True, check=True,
    )


def _drop_db(name: str) -> None:
    subprocess.run(
        ["psql", "-h", CONN["host"], "-p", CONN["port"], "-U", CONN["user"],
         "-d", "postgres", "-c", f"DROP DATABASE IF EXISTS \"{name}\";"],
        env=_pg_env(), capture_output=True,
    )


def _make_real_dump(dump_path: Path, src_db: str) -> None:
    """pg_dump the source DB into a custom-format dump file."""
    subprocess.run(
        ["pg_dump", "-h", CONN["host"], "-p", CONN["port"], "-U", CONN["user"],
         "-d", src_db, "-Fc", "-f", str(dump_path)],
        env=_pg_env(), capture_output=True, check=True,
    )


def _build_backup_dir(
    backup_dir: Path,
    export_dir: Path,
    source_artifact_dir: Path,
    src_db: str,
    timestamp: str = "20260625_010203",
) -> tuple[Path, str, str]:
    """Construct a realistic backup directory matching backup.sh's output.

    Returns (manifest_path, exports_checksum, source_checksum).
    """
    # Create a real pg_dump so pg_restore in restore.sh succeeds
    dump_name = f"classyfire_{timestamp}.dump"
    dump_path = backup_dir / dump_name
    _make_real_dump(dump_path, src_db)
    dump_checksum = _sha256_file(dump_path)

    # Create the artifact archives
    exports_archive_name = f"classyfire_exports_{timestamp}.tar.gz"
    source_archive_name = f"classyfire_source_artifacts_{timestamp}.tar.gz"
    exports_archive_path = backup_dir / exports_archive_name
    source_archive_path = backup_dir / source_archive_name

    _make_tar_gz(exports_archive_path, export_dir)
    _make_tar_gz(source_archive_path, source_artifact_dir)

    exports_checksum = _sha256_file(exports_archive_path)
    source_checksum = _sha256_file(source_archive_path)

    # Write the manifest in the EXACT format backup.sh produces.
    # These are single-line JSON entries inside the artifact_archives block —
    # the format that triggers the awk $4 vs $6 bug.
    manifest_content = textwrap.dedent(f"""\
        {{
          "timestamp": "2026-06-25T01:02:03Z",
          "file": "{dump_name}",
          "checksum": "{dump_checksum}",
          "media_file": null,
          "media_checksum": null,
          "artifact_archives": {{
            "media": null,
            "exports": {{"file": "{exports_archive_name}", "checksum": "{exports_checksum}"}},
            "source_artifacts": {{"file": "{source_archive_name}", "checksum": "{source_checksum}"}}
          }},
          "status": "ok"
        }}
    """)

    manifest_name = f"classyfire_{timestamp}.manifest.json"
    manifest_path = backup_dir / manifest_name
    manifest_path.write_text(manifest_content)

    return manifest_path, exports_checksum, source_checksum


# ---------------------------------------------------------------------------
# The core regression test
# ---------------------------------------------------------------------------


def test_restore_sh_extracts_artifact_archives() -> None:
    """restore.sh must extract exports and source_artifacts archives when
    RESTORE_EXPORT_DIR and RESTORE_SOURCE_ARTIFACT_DIR are set.

    This test FAILS against the original buggy awk ($4 → literal 'file')
    and PASSES after the fix ($i=="file" → $(i+2)).
    """
    src_db = "cf_c1_regression_src"
    dst_db = "cf_c1_regression_dst"
    _create_db(src_db)
    _create_db(dst_db)

    try:
        with tempfile.TemporaryDirectory(prefix="cf_c1_regression_") as tmpdir:
            tmp = Path(tmpdir)
            backup_dir = tmp / "backups"
            backup_dir.mkdir()

            # Source dirs with known sentinel files
            export_dir = tmp / "export_files"
            export_dir.mkdir()
            sentinel_export = export_dir / "my_export.csv"
            sentinel_export.write_text("col1,col2\nval1,val2\n")

            source_artifact_dir = tmp / "source_artifacts"
            source_artifact_dir.mkdir()
            sentinel_source = source_artifact_dir / "source_data.json"
            sentinel_source.write_text('{"kind": "LAB_OFFICIAL_IMPORT"}\n')

            # Build backup dir with manifest + archives + a real pg dump
            manifest_path, _exports_checksum, _source_checksum = _build_backup_dir(
                backup_dir, export_dir, source_artifact_dir, src_db
            )

            # Restore destination dirs (fresh, empty)
            restore_export_dir = tmp / "restored_exports"
            restore_source_dir = tmp / "restored_source_artifacts"

            env = _pg_env()
            env["POSTGRES_HOST"] = CONN["host"]
            env["POSTGRES_PORT"] = CONN["port"]
            env["POSTGRES_USER"] = CONN["user"]
            env["POSTGRES_DB"] = dst_db
            env["BACKUP_DIR"] = str(backup_dir)
            env["MANIFEST_FILE"] = manifest_path.name
            env["RESTORE_EXPORT_DIR"] = str(restore_export_dir)
            env["RESTORE_SOURCE_ARTIFACT_DIR"] = str(restore_source_dir)

            result = subprocess.run(
                ["sh", str(RESTORE_SH)],
                capture_output=True,
                text=True,
                env=env,
            )

            stdout = result.stdout
            stderr = result.stderr

            # ── Assertions for exports ─────────────────────────────────────
            assert "[restore] WARNING: exports archive not found" not in stdout, (
                "restore.sh reported exports archive not found — "
                "likely still using buggy awk ($4 == 'file' key, not value).\n"
                f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )

            assert "Exports restore skipped" not in stdout, (
                "restore.sh skipped exports even though RESTORE_EXPORT_DIR was set.\n"
                f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )

            assert "[restore] Exports checksum OK" in stdout, (
                "restore.sh did not reach exports checksum verification — "
                "EXPORTS_FILE was not correctly parsed from the manifest.\n"
                "Likely cause: buggy awk extracted field $4 ('file' key literal) "
                "instead of the actual filename value.\n"
                f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )

            assert "[restore] Exports restore complete." in stdout, (
                "restore.sh did not complete exports restore.\n"
                f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )

            # ── Assertions for source_artifacts ───────────────────────────
            assert "[restore] WARNING: source artifacts archive not found" not in stdout, (
                "restore.sh reported source_artifacts archive not found — "
                "likely still using buggy awk.\n"
                f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )

            assert "Source artifacts restore skipped" not in stdout, (
                "restore.sh skipped source_artifacts even though "
                "RESTORE_SOURCE_ARTIFACT_DIR was set.\n"
                f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )

            assert "[restore] Source artifacts checksum OK" in stdout, (
                "restore.sh did not reach source_artifacts checksum verification — "
                "SOURCE_ARTIFACTS_FILE was not correctly parsed from the manifest.\n"
                f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )

            assert "[restore] Source artifacts restore complete." in stdout, (
                "restore.sh did not complete source_artifacts restore.\n"
                f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )

            # ── Verify the actual files were extracted ─────────────────────
            # The validated archive root is stripped into the configured target.
            extracted_export = restore_export_dir / "my_export.csv"
            assert extracted_export.exists(), (
                f"Sentinel export file not found at {extracted_export} after restore.\n"
                f"restore.sh may have silently skipped the archive.\n"
                f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )

            extracted_source = restore_source_dir / "source_data.json"
            assert extracted_source.exists(), (
                f"Sentinel source artifact not found at {extracted_source} after restore.\n"
                f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )

            # Verify restore.sh exited successfully (all steps passed)
            assert result.returncode == 0, (
                f"restore.sh exited with code {result.returncode}\n"
                f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )

    finally:
        _drop_db(src_db)
        _drop_db(dst_db)
