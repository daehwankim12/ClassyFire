from __future__ import annotations

import pytest

from apps.operations.backup import BackupConfigurationError, validate_artifact_dir


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
