"""Portable backup bundle export/import helpers.

The regular backup command writes a manifest plus one or more files into the
backup store.  This module packages that manifest set into one operator-friendly
tarball and imports it back into a backup store after checksum verification.
It deliberately does not restore the live database.
"""
from __future__ import annotations

import json
import tarfile
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from shutil import copyfileobj, move
from typing import Any

from django.conf import settings

from apps.operations.backup import _sha256_file

BUNDLE_INDEX_FILENAME = "bundle-manifest.json"


class BackupBundleError(ValueError):
    """A backup bundle is missing, unsafe, corrupt, or checksum-invalid."""


@dataclass(frozen=True)
class _ManifestFile:
    name: str
    checksum: str | None


def _backup_dir(path: Path | None) -> Path:
    return path if path is not None else Path(getattr(settings, "BACKUP_DIR", "/backups"))


def _safe_filename(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BackupBundleError(f"{label} is missing")
    if "\\" in value:
        raise BackupBundleError(f"{label} is unsafe: {value}")
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or len(candidate.parts) != 1
        or candidate.name != value
        or value in {".", ".."}
    ):
        raise BackupBundleError(f"{label} is unsafe: {value}")
    return value


def _safe_child_path(directory: Path, filename: str) -> Path:
    name = _safe_filename(filename, label="filename")
    root = directory.resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise BackupBundleError(f"filename escapes backup directory: {filename}")
    return path


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupBundleError(f"invalid backup manifest: {path.name}") from exc
    if not isinstance(data, dict):
        raise BackupBundleError(f"invalid backup manifest: {path.name}")
    return data


def _referenced_files(manifest: dict[str, Any]) -> list[_ManifestFile]:
    files: list[_ManifestFile] = []

    dump_file = _safe_filename(manifest.get("file"), label="manifest file")
    dump_checksum = manifest.get("checksum")
    files.append(
        _ManifestFile(
            dump_file,
            dump_checksum if isinstance(dump_checksum, str) and dump_checksum else None,
        )
    )

    media_file = manifest.get("media_file")
    if isinstance(media_file, str) and media_file:
        media_checksum = manifest.get("media_checksum")
        files.append(
            _ManifestFile(
                _safe_filename(media_file, label="media_file"),
                media_checksum
                if isinstance(media_checksum, str) and media_checksum
                else None,
            )
        )

    artifact_archives = manifest.get("artifact_archives") or {}
    if not isinstance(artifact_archives, dict):
        raise BackupBundleError("artifact_archives must be an object")
    for label, entry in artifact_archives.items():
        if entry in (None, "", "null"):
            continue
        if not isinstance(entry, dict):
            raise BackupBundleError(f"artifact archive {label} must be an object")
        archive_file = entry.get("file")
        if not archive_file:
            continue
        checksum = entry.get("checksum")
        files.append(
            _ManifestFile(
                _safe_filename(archive_file, label=f"{label}.file"),
                checksum if isinstance(checksum, str) and checksum else None,
            )
        )

    seen: set[str] = set()
    unique: list[_ManifestFile] = []
    for item in files:
        if item.name in seen:
            continue
        seen.add(item.name)
        unique.append(item)
    return unique


def _latest_manifest(backup_dir: Path) -> str:
    manifests = sorted(backup_dir.glob("*.manifest.json"), reverse=True)
    if not manifests:
        raise BackupBundleError(f"no backup manifests found in {backup_dir}")
    return manifests[0].name


def verify_backup_manifest_files(
    backup_dir: Path | None = None,
    manifest_file: str | None = None,
) -> dict[str, Any]:
    """Verify that a backup manifest and every referenced file exist and match."""
    root = _backup_dir(backup_dir)
    manifest_name = (
        _safe_filename(manifest_file, label="manifest_file")
        if manifest_file
        else _latest_manifest(root)
    )
    manifest_path = _safe_child_path(root, manifest_name)
    if not manifest_path.exists():
        raise BackupBundleError(f"manifest not found: {manifest_name}")

    manifest = _load_manifest(manifest_path)
    verified: list[str] = []
    for item in _referenced_files(manifest):
        path = _safe_child_path(root, item.name)
        if not path.exists():
            raise BackupBundleError(f"referenced backup file not found: {item.name}")
        if item.checksum:
            actual = _sha256_file(path)
            if actual != item.checksum:
                raise BackupBundleError(
                    f"checksum mismatch for {item.name}: expected {item.checksum}, got {actual}"
                )
        verified.append(item.name)

    return {
        "manifest_file": manifest_name,
        "files": [manifest_name, *verified],
    }


def create_backup_bundle(
    output_path: Path,
    *,
    backup_dir: Path | None = None,
    manifest_file: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Create a portable tar.gz bundle for one backup manifest set."""
    root = _backup_dir(backup_dir)
    verified = verify_backup_manifest_files(root, manifest_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and not overwrite:
        raise BackupBundleError(f"bundle already exists: {output_path}")

    bundle_index = {
        "format": "classyfire-backup-bundle-v1",
        "manifest_file": verified["manifest_file"],
        "files": verified["files"],
    }
    with tarfile.open(output_path, "w:gz") as bundle:
        index_bytes = json.dumps(bundle_index, indent=2).encode()
        index_info = tarfile.TarInfo(BUNDLE_INDEX_FILENAME)
        index_info.size = len(index_bytes)
        bundle.addfile(index_info, fileobj=BytesIO(index_bytes))
        for name in verified["files"]:
            bundle.add(_safe_child_path(root, name), arcname=name)

    return {
        "bundle": str(output_path),
        "checksum": _sha256_file(output_path),
        "manifest_file": verified["manifest_file"],
        "files": verified["files"],
    }


def _safe_tar_member(member: tarfile.TarInfo) -> str:
    name = _safe_filename(member.name, label="bundle member")
    if not member.isfile():
        raise BackupBundleError(f"bundle member is not a regular file: {member.name}")
    return name


def import_backup_bundle(
    bundle_path: Path,
    *,
    backup_dir: Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Import a portable bundle into the backup store and verify it.

    This is intentionally non-destructive: it does not run pg_restore or touch
    the active database.
    """
    if not bundle_path.exists():
        raise BackupBundleError(f"bundle not found: {bundle_path}")
    root = _backup_dir(backup_dir)
    root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".backup-bundle-import-", dir=root) as tmp:
        staging = Path(tmp)
        try:
            with tarfile.open(bundle_path, "r:gz") as bundle:
                members = bundle.getmembers()
                safe_names = [_safe_tar_member(member) for member in members]
                manifest_names = [
                    name
                    for name in safe_names
                    if name.endswith(".manifest.json") and name != BUNDLE_INDEX_FILENAME
                ]
                if len(manifest_names) != 1:
                    raise BackupBundleError("bundle must contain exactly one backup manifest")
                for member, name in zip(members, safe_names, strict=True):
                    if name == BUNDLE_INDEX_FILENAME:
                        continue
                    target = _safe_child_path(root, name)
                    if target.exists() and not overwrite:
                        raise BackupBundleError(f"backup file already exists: {name}")
                    source = bundle.extractfile(member)
                    if source is None:
                        raise BackupBundleError(f"could not read bundle member: {name}")
                    with (staging / name).open("wb") as destination:
                        copyfileobj(source, destination)
        except tarfile.TarError as exc:
            raise BackupBundleError(f"invalid backup bundle: {bundle_path}") from exc

        manifest_name = manifest_names[0]
        verified = verify_backup_manifest_files(staging, manifest_name)
        for name in verified["files"]:
            destination_path = _safe_child_path(root, name)
            if destination_path.exists() and overwrite:
                destination_path.unlink()
            move(str(staging / name), destination_path)
    return {
        "bundle": str(bundle_path),
        "bundle_checksum": _sha256_file(bundle_path),
        "backup_dir": str(root),
        "manifest_file": manifest_name,
        "files": verified["files"],
    }
