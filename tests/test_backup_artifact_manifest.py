from pathlib import Path

import pytest


def test_compose_backup_mounts_export_and_source_volumes():
    compose = Path("docker-compose.yml").read_text()
    assert "- export_files:/app/export_files:ro" in compose
    assert "- source_artifacts:/app/source_artifacts:ro" in compose


@pytest.mark.django_db
def test_python_backup_manifest_lists_artifact_archives(tmp_path, settings, monkeypatch):
    # Artifact dirs must NOT be nested inside backup_dir (validate_artifact_dir rejects that).
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    app_root = tmp_path / "app"
    app_root.mkdir()
    settings.BACKUP_DIR = str(backup_dir)
    settings.MEDIA_ROOT = str(app_root / "media")
    settings.EXPORT_DIR = str(app_root / "export_files")
    settings.SOURCE_ARTIFACT_DIR = str(app_root / "source_artifacts")
    Path(settings.MEDIA_ROOT).mkdir()
    Path(settings.EXPORT_DIR).mkdir()
    Path(settings.SOURCE_ARTIFACT_DIR).mkdir()
    (Path(settings.EXPORT_DIR) / "x.csv").write_text("a,b\n")
    (Path(settings.SOURCE_ARTIFACT_DIR) / "source.json").write_text("{}")

    def fake_dump(cmd, dump_path):
        dump_path.write_text("dump")

    from apps.operations.backup import run_backup_injectable

    manifest = run_backup_injectable(backup_dir, fake_dump)

    assert manifest["artifact_archives"]["exports"]["checksum"]
    assert manifest["artifact_archives"]["source_artifacts"]["checksum"]
