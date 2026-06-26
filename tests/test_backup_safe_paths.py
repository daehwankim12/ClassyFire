from __future__ import annotations

import os
import subprocess

import pytest

from apps.operations.backup import BackupConfigurationError, validate_artifact_dir


@pytest.mark.parametrize(
    ("env_key", "env_value"),
    [
        ("MEDIA_DIR", "."),
        ("UPLOAD_DIR", "."),
        ("UPLOAD_DIR", "{backup_dir}/uploads"),
        ("BACKUP_DIR", "{upload_dir}/nested-backup"),
    ],
)
def test_production_backup_script_rejects_unsafe_paths(
    tmp_path,
    monkeypatch,
    env_key,
    env_value,
):
    """The production shell backup must fail-closed on unsafe artifact roots.

    Mirrors the Python ``validate_artifact_dir`` guards: a path that resolves to
    cwd/``/``, an artifact dir inside the backup dir, or a backup dir inside an
    artifact dir must abort BEFORE any dump is written.
    """
    backup_dir = tmp_path / "backups"
    media_dir = tmp_path / "media"
    upload_dir = tmp_path / "uploads"
    backup_dir.mkdir()
    media_dir.mkdir()
    upload_dir.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "pg_dump").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "pg_dump").chmod(0o755)

    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "POSTGRES_HOST": "db",
        "POSTGRES_USER": "classyfire",
        "POSTGRES_DB": "classyfire",
        "PGPASSWORD": "pw",
        "BACKUP_DIR": str(backup_dir),
        "MEDIA_DIR": str(media_dir),
        "EXPORT_DIR": str(tmp_path / "exports"),
        "SOURCE_ARTIFACT_DIR": str(tmp_path / "source_artifacts"),
        "UPLOAD_DIR": str(upload_dir),
    }
    env[env_key] = env_value.format(backup_dir=backup_dir, upload_dir=upload_dir)
    result = subprocess.run(
        ["sh", "deploy/backup/backup.sh"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert not list(backup_dir.glob("*.dump"))


@pytest.mark.parametrize("value", ["", ".", "/"])
def test_backup_rejects_empty_cwd_and_root(value, tmp_path):
    with pytest.raises(BackupConfigurationError):
        validate_artifact_dir(value, backup_dir=tmp_path / "backups", label="media")


def test_backup_rejects_backup_parent(tmp_path):
    backup_dir = tmp_path / "media" / "backups"
    with pytest.raises(BackupConfigurationError):
        validate_artifact_dir(str(tmp_path / "media"), backup_dir=backup_dir, label="media")


def test_backup_accepts_dedicated_artifact_dir(tmp_path):
    artifact_dir = tmp_path / "media"
    artifact_dir.mkdir()
    resolved = validate_artifact_dir(str(artifact_dir), backup_dir=tmp_path / "backups", label="media")
    assert resolved == artifact_dir.resolve()


def test_backup_rejects_symlink_to_root(tmp_path):
    link = tmp_path / "media-link"
    link.symlink_to("/")
    with pytest.raises(BackupConfigurationError):
        validate_artifact_dir(str(link), backup_dir=tmp_path / "backups", label="media")


def test_backup_rejects_upload_root_inside_backup_root(tmp_path):
    backup_dir = tmp_path / "backups"
    upload_dir = backup_dir / "uploads"
    upload_dir.mkdir(parents=True)
    with pytest.raises(BackupConfigurationError):
        validate_artifact_dir(str(upload_dir), backup_dir=backup_dir, label="uploads")


def test_backup_allows_nonexistent_dedicated_artifact_dir(tmp_path):
    media = tmp_path / "dedicated-media"
    resolved = validate_artifact_dir(str(media), backup_dir=tmp_path / "backups", label="media")
    assert resolved == media.resolve()


def test_retention_rejects_manifest_path_traversal(tmp_path, settings):
    """Pruning must not unlink manifest-controlled paths outside backup_dir."""
    import json
    from datetime import UTC, datetime, timedelta

    from apps.operations.backup import apply_retention

    settings.BACKUP_DAILY_COUNT = 0
    victim = tmp_path / "victim.txt"
    victim.write_text("do not delete")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    manifest = {
        "timestamp": (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat(),
        "file": "../victim.txt",
        "media_file": "/tmp/also-unsafe.tar.gz",
        "artifact_archives": {"exports": {"file": "../victim.txt"}},
        "checksum": "abc",
        "status": "ok",
    }
    (backup_dir / "classyfire_unsafe.manifest.json").write_text(json.dumps(manifest))

    apply_retention(backup_dir, now=datetime.now(tz=UTC))

    assert victim.read_text() == "do not delete"
    assert not (backup_dir / "classyfire_unsafe.manifest.json").exists()


def test_production_backup_script_validates_manifest_dump_name():
    script = os.path.join("deploy", "backup", "backup.sh")
    text = open(script, encoding="utf-8").read()

    assert "_safe_backup_file()" in text
    assert '""|/*|*/*|*\\\\*)' in text
    assert 'DUMP_PATH="$(_safe_backup_file "${DUMP_NAME}"' in text
    assert 'rm "${BACKUP_DIR}/${DUMP_NAME}"' not in text
