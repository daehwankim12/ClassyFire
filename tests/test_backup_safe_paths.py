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
