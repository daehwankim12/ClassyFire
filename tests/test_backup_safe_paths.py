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
