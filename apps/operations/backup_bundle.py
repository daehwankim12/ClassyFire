"""Portable backup bundle export/import helpers.

The regular backup command writes a manifest plus one or more files into the
backup store.  This module packages that manifest set into one operator-friendly
tarball and imports it back into a backup store after checksum verification.
It deliberately does not restore the live database.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import stat
import tarfile
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from shutil import copyfileobj, move
from typing import Any, BinaryIO, cast

from django.conf import settings

from apps.operations.backup import _sha256_file

BUNDLE_INDEX_FILENAME = "bundle-manifest.json"
BUNDLE_FORMAT = "classyfire-backup-bundle-v1"
DEFAULT_MAX_BUNDLE_MEMBER_SIZE = 10 * 1024 * 1024 * 1024
DEFAULT_MAX_BUNDLE_TOTAL_SIZE = 25 * 1024 * 1024 * 1024
DEFAULT_MAX_BUNDLE_COMPRESSED_SIZE = 26 * 1024 * 1024 * 1024
DEFAULT_MAX_BUNDLE_MEMBERS = 64
MAX_BUNDLE_METADATA_SIZE = 64 * 1024


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
        "format": BUNDLE_FORMAT,
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


def _bundle_size_limit(setting_name: str, default: int) -> int:
    value = getattr(settings, setting_name, default)
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise BackupBundleError(f"{setting_name} must be an integer") from exc
    if limit <= 0:
        raise BackupBundleError(f"{setting_name} must be positive")
    return limit


def _safe_tar_member(member: tarfile.TarInfo) -> str:
    name = _safe_filename(member.name, label="bundle member")
    if not member.isfile():
        raise BackupBundleError(f"bundle member is not a regular file: {member.name}")
    return name


class _BoundedReader:
    def __init__(self, source: BinaryIO, limit: int) -> None:
        self._source = source
        self._remaining = limit

    def read(self, size: int = -1) -> bytes:
        read_size = self._remaining + 1 if size < 0 else min(size, self._remaining + 1)
        data = self._source.read(read_size)
        if len(data) > self._remaining:
            raise BackupBundleError("compressed bundle exceeds maximum size")
        self._remaining -= len(data)
        return data


def _bounded_tar_info(max_members: int) -> type[tarfile.TarInfo]:
    class _BoundedTarInfo(tarfile.TarInfo):
        def _proc_member(self, bundle: tarfile.TarFile) -> tarfile.TarInfo:
            member_count = getattr(bundle, "_bundle_member_count", 0) + 1
            bundle._bundle_member_count = member_count
            if member_count > max_members:
                raise BackupBundleError("bundle contains too many members")
            metadata_types = {
                tarfile.GNUTYPE_LONGNAME,
                tarfile.GNUTYPE_LONGLINK,
                tarfile.XHDTYPE,
                tarfile.XGLTYPE,
                tarfile.SOLARIS_XHDTYPE,
            }
            if self.type in metadata_types and self.size > MAX_BUNDLE_METADATA_SIZE:
                raise BackupBundleError("bundle metadata header is too large")
            if self.type == tarfile.GNUTYPE_SPARSE:
                raise BackupBundleError("bundle sparse members are not supported")
            return cast(
                tarfile.TarInfo,
                super()._proc_member(bundle),  # type: ignore[attr-defined]
            )

    return _BoundedTarInfo


def _load_bundle_index(
    bundle: tarfile.TarFile,
    member: tarfile.TarInfo,
    *,
    max_members: int,
) -> tuple[str, list[str]]:
    if member.size > MAX_BUNDLE_METADATA_SIZE:
        raise BackupBundleError("bundle index is too large")
    source = bundle.extractfile(member)
    if source is None:
        raise BackupBundleError("could not read bundle index")
    try:
        with source:
            data = json.loads(source.read())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupBundleError("invalid bundle index") from exc
    if not isinstance(data, dict) or data.get("format") != BUNDLE_FORMAT:
        raise BackupBundleError("invalid bundle index")

    manifest_name = _safe_filename(data.get("manifest_file"), label="bundle manifest_file")
    if not manifest_name.endswith(".manifest.json"):
        raise BackupBundleError("bundle manifest_file must name a backup manifest")
    files = data.get("files")
    if not isinstance(files, list):
        raise BackupBundleError("bundle index files must be a list")
    indexed_names = [_safe_filename(name, label="bundle index file") for name in files]
    if not indexed_names or indexed_names[0] != manifest_name:
        raise BackupBundleError("bundle manifest must be first in index files")
    if BUNDLE_INDEX_FILENAME in indexed_names:
        raise BackupBundleError("bundle index cannot list itself as a payload")
    if len(indexed_names) != len(set(indexed_names)):
        raise BackupBundleError("bundle index contains duplicate file names")
    if len(indexed_names) + 1 > max_members:
        raise BackupBundleError("bundle contains too many members")
    return manifest_name, indexed_names


def _trusted_bundle_copy(
    bundle_path: Path,
    destination: Path,
    expected_checksum: str,
) -> str:
    if not isinstance(expected_checksum, str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", expected_checksum
    ):
        raise BackupBundleError("expected bundle checksum must be a 64-character SHA-256")

    max_compressed_size = _bundle_size_limit(
        "BACKUP_BUNDLE_MAX_COMPRESSED_SIZE",
        DEFAULT_MAX_BUNDLE_COMPRESSED_SIZE,
    )
    try:
        digest = hashlib.sha256()
        with bundle_path.open("rb") as source:
            # Refuse non-regular and oversized input before copying it in full,
            # and bound the read itself in case the file grows while we copy.
            source_stat = os.fstat(source.fileno())
            if not stat.S_ISREG(source_stat.st_mode):
                raise BackupBundleError("bundle must be a regular file")
            if source_stat.st_size > max_compressed_size:
                raise BackupBundleError("compressed bundle exceeds maximum size")
            bounded = _BoundedReader(cast(BinaryIO, source), max_compressed_size)
            with destination.open("wb") as target:
                while chunk := bounded.read(1024 * 1024):
                    target.write(chunk)
                    digest.update(chunk)
    except OSError as exc:
        raise BackupBundleError(f"could not read backup bundle: {bundle_path}") from exc

    actual_checksum = digest.hexdigest()
    if not hmac.compare_digest(actual_checksum, expected_checksum.lower()):
        raise BackupBundleError(
            "bundle checksum mismatch: "
            f"expected {expected_checksum.lower()}, got {actual_checksum}"
        )
    return actual_checksum


def _import_authenticated_bundle(
    bundle_path: Path,
    *,
    display_path: Path,
    backup_dir: Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Stage a private bundle copy that already matched its trusted checksum."""
    root = _backup_dir(backup_dir)
    root.mkdir(parents=True, exist_ok=True)

    max_member_size = _bundle_size_limit(
        "BACKUP_BUNDLE_MAX_MEMBER_SIZE",
        DEFAULT_MAX_BUNDLE_MEMBER_SIZE,
    )
    max_total_size = _bundle_size_limit(
        "BACKUP_BUNDLE_MAX_TOTAL_SIZE",
        DEFAULT_MAX_BUNDLE_TOTAL_SIZE,
    )
    max_compressed_size = _bundle_size_limit(
        "BACKUP_BUNDLE_MAX_COMPRESSED_SIZE",
        DEFAULT_MAX_BUNDLE_COMPRESSED_SIZE,
    )
    max_members = _bundle_size_limit(
        "BACKUP_BUNDLE_MAX_MEMBERS",
        DEFAULT_MAX_BUNDLE_MEMBERS,
    )

    with tempfile.TemporaryDirectory(prefix=".backup-bundle-import-", dir=root) as tmp:
        staging = Path(tmp)
        manifest_name: str | None = None
        try:
            with bundle_path.open("rb") as bundle_source:
                bundle_stat = os.fstat(bundle_source.fileno())
                if not stat.S_ISREG(bundle_stat.st_mode):
                    raise BackupBundleError("bundle must be a regular file")
                if bundle_stat.st_size > max_compressed_size:
                    raise BackupBundleError("compressed bundle exceeds maximum size")
                bounded_source = _BoundedReader(bundle_source, max_compressed_size)
                with tarfile.open(
                    fileobj=bounded_source,
                    mode="r|gz",
                    tarinfo=_bounded_tar_info(max_members),
                ) as bundle:
                    seen_names: set[str] = set()
                    indexed_names: list[str] | None = None
                    expected_names: set[str] | None = None
                    manifest_loaded = False
                    total_size = 0
                    for position, member in enumerate(bundle, start=1):
                        name = _safe_tar_member(member)
                        if name in seen_names:
                            raise BackupBundleError("bundle contains duplicate member names")
                        seen_names.add(name)

                        if position == 1:
                            if name != BUNDLE_INDEX_FILENAME:
                                raise BackupBundleError("bundle index must be the first member")
                            manifest_name, indexed_names = _load_bundle_index(
                                bundle,
                                member,
                                max_members=max_members,
                            )
                            expected_names = set(indexed_names)
                            continue

                        if manifest_name is None or expected_names is None:
                            raise BackupBundleError("bundle is missing its index")
                        if position == 2 and name != manifest_name:
                            raise BackupBundleError("bundle manifest must follow the bundle index")
                        if name not in expected_names:
                            raise BackupBundleError(f"bundle contains unexpected member: {name}")
                        if member.size > max_member_size:
                            raise BackupBundleError(f"bundle member is too large: {name}")
                        total_size += member.size
                        if total_size > max_total_size:
                            raise BackupBundleError("bundle contents exceed maximum total size")
                        target = _safe_child_path(root, name)
                        if target.exists() and not overwrite:
                            raise BackupBundleError(f"backup file already exists: {name}")
                        source = bundle.extractfile(member)
                        if source is None:
                            raise BackupBundleError(f"could not read bundle member: {name}")
                        with source, (staging / name).open("wb") as destination:
                            copyfileobj(source, destination)

                        if name == manifest_name:
                            manifest = _load_manifest(staging / manifest_name)
                            manifest_names = {
                                manifest_name,
                                *[item.name for item in _referenced_files(manifest)],
                            }
                            if manifest_names != expected_names:
                                raise BackupBundleError(
                                    "bundle index files do not match the backup manifest"
                                )
                            manifest_loaded = True

                    if manifest_name is None or indexed_names is None or expected_names is None:
                        raise BackupBundleError("bundle is missing its index")
                    if not manifest_loaded:
                        raise BackupBundleError("bundle is missing its backup manifest")
                    actual_names = seen_names - {BUNDLE_INDEX_FILENAME}
                    missing_names = expected_names - actual_names
                    if missing_names:
                        raise BackupBundleError(
                            f"bundle is missing referenced member: {sorted(missing_names)[0]}"
                        )
        except tarfile.TarError as exc:
            raise BackupBundleError(f"invalid backup bundle: {display_path}") from exc

        if manifest_name is None:
            raise BackupBundleError("bundle is missing its backup manifest")
        verified = verify_backup_manifest_files(staging, manifest_name)
        for name in verified["files"]:
            destination_path = _safe_child_path(root, name)
            if destination_path.exists() and overwrite:
                destination_path.unlink()
            move(str(staging / name), destination_path)
    return {
        "backup_dir": str(root),
        "manifest_file": manifest_name,
        "files": verified["files"],
    }


def import_backup_bundle(
    bundle_path: Path,
    *,
    expected_checksum: str,
    backup_dir: Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Authenticate and import a portable bundle into the backup store.

    This is intentionally non-destructive: it does not run pg_restore or touch
    the active database.
    """
    if not bundle_path.exists():
        raise BackupBundleError(f"bundle not found: {bundle_path}")

    # Quarantine the private copy inside the backup store rather than the system
    # TMPDIR: the store is already sized for (and used to stage) bundle contents,
    # whereas a container's /tmp lives on the writable layer and may already hold
    # the operator's `docker cp`'d bundle, doubling a potentially very large file.
    root = _backup_dir(backup_dir)
    created_root = not root.exists()
    root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(
            prefix=".backup-bundle-auth-", dir=root
        ) as auth_tmp:
            authenticated_bundle = Path(auth_tmp) / "bundle.tar.gz"
            bundle_checksum = _trusted_bundle_copy(
                bundle_path,
                authenticated_bundle,
                expected_checksum,
            )
            imported = _import_authenticated_bundle(
                authenticated_bundle,
                display_path=bundle_path,
                backup_dir=backup_dir,
                overwrite=overwrite,
            )
    except BaseException:
        # A rejected bundle must not leave a backup directory behind that this
        # call created; only an empty directory is removed, never real backups.
        if created_root:
            with contextlib.suppress(OSError):
                root.rmdir()
        raise

    return {
        "bundle": str(bundle_path),
        "bundle_checksum": bundle_checksum,
        **imported,
    }
