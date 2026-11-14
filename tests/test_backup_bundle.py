from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _write_file(path: Path, content: bytes) -> str:
    path.write_bytes(content)
    return _sha256(path)


def _write_backup_fixture(backup_dir: Path) -> str:
    dump_checksum = _write_file(backup_dir / "classyfire_20260627_010203.dump", b"dump")
    exports_checksum = _write_file(
        backup_dir / "classyfire_exports_20260627_010203.tar.gz",
        b"exports",
    )
    uploads_checksum = _write_file(
        backup_dir / "classyfire_uploads_20260627_010203.tar.gz",
        b"uploads",
    )
    manifest = {
        "timestamp": "2026-06-27T01:02:03+00:00",
        "file": "classyfire_20260627_010203.dump",
        "checksum": dump_checksum,
        "artifact_archives": {
            "media": None,
            "exports": {
                "file": "classyfire_exports_20260627_010203.tar.gz",
                "checksum": exports_checksum,
            },
            "source_artifacts": None,
            "uploads": {
                "file": "classyfire_uploads_20260627_010203.tar.gz",
                "checksum": uploads_checksum,
            },
        },
        "status": "ok",
    }
    manifest_name = "classyfire_20260627_010203.manifest.json"
    (backup_dir / manifest_name).write_text(json.dumps(manifest, indent=2))
    return manifest_name


def _add_bundle_index(
    bundle: tarfile.TarFile,
    manifest_name: str,
    files: list[str],
) -> None:
    index = {
        "format": "classyfire-backup-bundle-v1",
        "manifest_file": manifest_name,
        "files": files,
    }
    index_bytes = json.dumps(index).encode()
    index_info = tarfile.TarInfo("bundle-manifest.json")
    index_info.size = len(index_bytes)
    bundle.addfile(index_info, io.BytesIO(index_bytes))


def test_create_backup_bundle_packages_manifest_and_referenced_files(tmp_path: Path):
    from apps.operations.backup_bundle import create_backup_bundle

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    manifest_name = _write_backup_fixture(backup_dir)
    output = tmp_path / "portable" / "classyfire-backup.tar.gz"

    result = create_backup_bundle(output, backup_dir=backup_dir)

    assert result["bundle"] == str(output)
    assert result["checksum"] == _sha256(output)
    assert result["manifest_file"] == manifest_name

    with tarfile.open(output, "r:gz") as bundle:
        names = bundle.getnames()

    assert names[:2] == ["bundle-manifest.json", manifest_name]
    assert manifest_name in names
    assert "classyfire_20260627_010203.dump" in names
    assert "classyfire_exports_20260627_010203.tar.gz" in names
    assert "classyfire_uploads_20260627_010203.tar.gz" in names


def test_import_backup_bundle_extracts_and_verifies_files(tmp_path: Path):
    from apps.operations.backup_bundle import create_backup_bundle, import_backup_bundle

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    manifest_name = _write_backup_fixture(source_dir)
    bundle_path = tmp_path / "classyfire-backup.tar.gz"
    create_backup_bundle(bundle_path, backup_dir=source_dir)

    target_dir = tmp_path / "target"
    result = import_backup_bundle(
        bundle_path,
        expected_checksum=_sha256(bundle_path),
        backup_dir=target_dir,
    )

    assert result["manifest_file"] == manifest_name
    assert result["bundle_checksum"] == _sha256(bundle_path)
    assert (target_dir / manifest_name).exists()
    assert (target_dir / "classyfire_20260627_010203.dump").exists()
    assert (target_dir / "classyfire_exports_20260627_010203.tar.gz").exists()
    assert (target_dir / "classyfire_uploads_20260627_010203.tar.gz").exists()
    assert not (target_dir / "bundle-manifest.json").exists()


def test_import_backup_bundle_rejects_checksum_mismatch(tmp_path: Path):
    from apps.operations.backup_bundle import BackupBundleError, import_backup_bundle

    manifest = {
        "timestamp": "2026-06-27T01:02:03+00:00",
        "file": "classyfire_20260627_010203.dump",
        "checksum": "0" * 64,
        "artifact_archives": {},
        "status": "ok",
    }
    bundle_path = tmp_path / "bad-checksum.tar.gz"
    with tarfile.open(bundle_path, "w:gz") as bundle:
        manifest_name = "classyfire_20260627_010203.manifest.json"
        dump_name = "classyfire_20260627_010203.dump"
        _add_bundle_index(bundle, manifest_name, [manifest_name, dump_name])
        manifest_bytes = json.dumps(manifest).encode()
        manifest_info = tarfile.TarInfo(manifest_name)
        manifest_info.size = len(manifest_bytes)
        bundle.addfile(manifest_info, io.BytesIO(manifest_bytes))
        dump_info = tarfile.TarInfo(dump_name)
        dump_info.size = len(b"dump")
        bundle.addfile(dump_info, io.BytesIO(b"dump"))

    with pytest.raises(BackupBundleError, match="checksum"):
        import_backup_bundle(
            bundle_path,
            expected_checksum=_sha256(bundle_path),
            backup_dir=tmp_path / "target",
        )

    assert not (tmp_path / "target" / "classyfire_20260627_010203.dump").exists()


def test_import_backup_bundle_rejects_unsafe_tar_member(tmp_path: Path):
    from apps.operations.backup_bundle import BackupBundleError, import_backup_bundle

    bundle_path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(bundle_path, "w:gz") as bundle:
        manifest_name = "classyfire_20260627_010203.manifest.json"
        _add_bundle_index(bundle, manifest_name, [manifest_name])
        payload = b"owned"
        info = tarfile.TarInfo("../outside.txt")
        info.size = len(payload)
        bundle.addfile(info, io.BytesIO(payload))

    with pytest.raises(BackupBundleError, match="unsafe"):
        import_backup_bundle(
            bundle_path,
            expected_checksum=_sha256(bundle_path),
            backup_dir=tmp_path / "target",
        )


def test_import_backup_bundle_rejects_unreferenced_members(tmp_path: Path):
    from apps.operations.backup_bundle import BackupBundleError, import_backup_bundle

    dump = b"dump"
    manifest = {
        "timestamp": "2026-06-27T01:02:03+00:00",
        "file": "classyfire_20260627_010203.dump",
        "checksum": hashlib.sha256(dump).hexdigest(),
        "artifact_archives": {},
        "status": "ok",
    }
    bundle_path = tmp_path / "extra-member.tar.gz"
    with tarfile.open(bundle_path, "w:gz") as bundle:
        manifest_name = "classyfire_20260627_010203.manifest.json"
        dump_name = "classyfire_20260627_010203.dump"
        _add_bundle_index(bundle, manifest_name, [manifest_name, dump_name])
        manifest_bytes = json.dumps(manifest).encode()
        manifest_info = tarfile.TarInfo(manifest_name)
        manifest_info.size = len(manifest_bytes)
        bundle.addfile(manifest_info, io.BytesIO(manifest_bytes))
        dump_info = tarfile.TarInfo(dump_name)
        dump_info.size = len(dump)
        bundle.addfile(dump_info, io.BytesIO(dump))
        extra_info = tarfile.TarInfo("unreferenced-extra.bin")
        extra_info.size = len(b"extra")
        bundle.addfile(extra_info, io.BytesIO(b"extra"))

    target_dir = tmp_path / "target"
    with pytest.raises(BackupBundleError, match="unexpected member"):
        import_backup_bundle(
            bundle_path,
            expected_checksum=_sha256(bundle_path),
            backup_dir=target_dir,
        )

    assert not (target_dir / "unreferenced-extra.bin").exists()


def test_import_backup_bundle_enforces_size_limit(tmp_path: Path, settings):
    from apps.operations.backup_bundle import BackupBundleError, import_backup_bundle

    settings.BACKUP_BUNDLE_MAX_MEMBER_SIZE = 3
    dump = b"dump"
    manifest = {
        "timestamp": "2026-06-27T01:02:03+00:00",
        "file": "classyfire_20260627_010203.dump",
        "checksum": hashlib.sha256(dump).hexdigest(),
        "artifact_archives": {},
        "status": "ok",
    }
    bundle_path = tmp_path / "too-large.tar.gz"
    with tarfile.open(bundle_path, "w:gz") as bundle:
        manifest_name = "classyfire_20260627_010203.manifest.json"
        dump_name = "classyfire_20260627_010203.dump"
        _add_bundle_index(bundle, manifest_name, [manifest_name, dump_name])
        manifest_bytes = json.dumps(manifest).encode()
        manifest_info = tarfile.TarInfo(manifest_name)
        manifest_info.size = len(manifest_bytes)
        bundle.addfile(manifest_info, io.BytesIO(manifest_bytes))
        dump_info = tarfile.TarInfo(dump_name)
        dump_info.size = len(dump)
        bundle.addfile(dump_info, io.BytesIO(dump))

    with pytest.raises(BackupBundleError, match="too large"):
        import_backup_bundle(
            bundle_path,
            expected_checksum=_sha256(bundle_path),
            backup_dir=tmp_path / "target",
        )


def test_import_backup_bundle_rejects_substituted_self_consistent_bundle(tmp_path: Path):
    from apps.operations.backup_bundle import (
        BackupBundleError,
        create_backup_bundle,
        import_backup_bundle,
    )

    trusted_source = tmp_path / "trusted-source"
    trusted_source.mkdir()
    _write_backup_fixture(trusted_source)
    trusted_bundle = tmp_path / "trusted.tar.gz"
    create_backup_bundle(trusted_bundle, backup_dir=trusted_source)

    substituted_source = tmp_path / "substituted-source"
    substituted_source.mkdir()
    manifest_name = _write_backup_fixture(substituted_source)
    dump_path = substituted_source / "classyfire_20260627_010203.dump"
    substituted_checksum = _write_file(dump_path, b"attacker-selected dump")
    manifest_path = substituted_source / manifest_name
    manifest = json.loads(manifest_path.read_text())
    manifest["checksum"] = substituted_checksum
    manifest_path.write_text(json.dumps(manifest, indent=2))
    substituted_bundle = tmp_path / "substituted.tar.gz"
    create_backup_bundle(substituted_bundle, backup_dir=substituted_source)

    target_dir = tmp_path / "target"
    with pytest.raises(BackupBundleError, match="bundle checksum mismatch"):
        import_backup_bundle(
            substituted_bundle,
            expected_checksum=_sha256(trusted_bundle),
            backup_dir=target_dir,
        )

    assert not target_dir.exists()


def test_import_backup_bundle_rejects_excessive_member_headers(tmp_path: Path, settings):
    from apps.operations.backup_bundle import BackupBundleError, import_backup_bundle

    settings.BACKUP_BUNDLE_MAX_MEMBERS = 3
    dump = b"dump"
    manifest_name = "classyfire_20260627_010203.manifest.json"
    dump_name = "classyfire_20260627_010203.dump"
    manifest = {
        "file": dump_name,
        "checksum": hashlib.sha256(dump).hexdigest(),
        "artifact_archives": {},
    }
    bundle_path = tmp_path / "too-many-members.tar.gz"
    with tarfile.open(bundle_path, "w:gz") as bundle:
        _add_bundle_index(bundle, manifest_name, [manifest_name, dump_name])
        manifest_bytes = json.dumps(manifest).encode()
        manifest_info = tarfile.TarInfo(manifest_name)
        manifest_info.size = len(manifest_bytes)
        bundle.addfile(manifest_info, io.BytesIO(manifest_bytes))
        dump_info = tarfile.TarInfo(dump_name)
        dump_info.size = len(dump)
        bundle.addfile(dump_info, io.BytesIO(dump))
        for index in range(100):
            bundle.addfile(tarfile.TarInfo(f"empty-{index}.txt"), io.BytesIO())

    target_dir = tmp_path / "target"
    with pytest.raises(BackupBundleError, match="too many members"):
        import_backup_bundle(
            bundle_path,
            expected_checksum=_sha256(bundle_path),
            backup_dir=target_dir,
        )

    assert not (target_dir / dump_name).exists()


def test_import_backup_bundle_enforces_compressed_size_limit(tmp_path: Path, settings):
    from apps.operations.backup_bundle import BackupBundleError, import_backup_bundle

    settings.BACKUP_BUNDLE_MAX_COMPRESSED_SIZE = 1
    manifest_name = "classyfire_20260627_010203.manifest.json"
    bundle_path = tmp_path / "compressed-too-large.tar.gz"
    with tarfile.open(bundle_path, "w:gz") as bundle:
        _add_bundle_index(bundle, manifest_name, [manifest_name])

    with pytest.raises(BackupBundleError, match="compressed bundle exceeds maximum size"):
        import_backup_bundle(
            bundle_path,
            expected_checksum=_sha256(bundle_path),
            backup_dir=tmp_path / "target",
        )

    # The size cap applies before the bundle is copied for authentication, so
    # an oversized input leaves no quarantine copy or target directory behind.
    assert not (tmp_path / "target").exists()


def test_export_backup_bundle_command_prints_bundle_path_and_checksum(tmp_path: Path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _write_backup_fixture(backup_dir)
    output = tmp_path / "out" / "backup.tar.gz"
    stdout = io.StringIO()

    call_command(
        "export_backup_bundle",
        "--backup-dir",
        str(backup_dir),
        "--output",
        str(output),
        stdout=stdout,
    )

    text = stdout.getvalue()
    assert "Backup bundle complete:" in text
    assert str(output) in text
    assert "Checksum:" in text


def test_import_backup_bundle_command_prints_restore_hint(tmp_path: Path):
    from apps.operations.backup_bundle import create_backup_bundle

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    manifest_name = _write_backup_fixture(source_dir)
    bundle_path = tmp_path / "backup.tar.gz"
    create_backup_bundle(bundle_path, backup_dir=source_dir)
    target_dir = tmp_path / "target"
    stdout = io.StringIO()

    call_command(
        "import_backup_bundle",
        str(bundle_path),
        "--checksum",
        _sha256(bundle_path),
        "--backup-dir",
        str(target_dir),
        stdout=stdout,
    )

    text = stdout.getvalue()
    assert f"Imported backup manifest: {manifest_name}" in text
    assert "deploy/backup/restore.sh" in text


def test_import_backup_bundle_command_hides_restore_hint_for_untrusted_bundle(tmp_path: Path):
    from apps.operations.backup_bundle import create_backup_bundle

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    _write_backup_fixture(source_dir)
    bundle_path = tmp_path / "backup.tar.gz"
    create_backup_bundle(bundle_path, backup_dir=source_dir)
    target_dir = tmp_path / "target"
    stdout = io.StringIO()

    with pytest.raises(CommandError, match="bundle checksum mismatch"):
        call_command(
            "import_backup_bundle",
            str(bundle_path),
            "--checksum",
            "0" * 64,
            "--backup-dir",
            str(target_dir),
            stdout=stdout,
        )

    assert "Restore hint:" not in stdout.getvalue()
    assert not target_dir.exists()
