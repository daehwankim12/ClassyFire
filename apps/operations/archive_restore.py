"""Validate and safely restore a backup artifact tar archive."""

from __future__ import annotations

import argparse
import itertools
import os
import shutil
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

DEFAULT_MAX_MEMBERS = 100_000
DEFAULT_MAX_EXPANDED_BYTES = 25 * 1024 * 1024 * 1024
DEFAULT_MAX_COMPRESSION_RATIO = 200
_COPY_CHUNK_SIZE = 1024 * 1024


class UnsafeArchiveError(ValueError):
    """An artifact archive violates the restore safety contract."""


@dataclass(frozen=True)
class ArchiveLimits:
    max_members: int = DEFAULT_MAX_MEMBERS
    max_expanded_bytes: int = DEFAULT_MAX_EXPANDED_BYTES
    max_compression_ratio: int = DEFAULT_MAX_COMPRESSION_RATIO

    def __post_init__(self) -> None:
        for name, value in (
            ("max_members", self.max_members),
            ("max_expanded_bytes", self.max_expanded_bytes),
            ("max_compression_ratio", self.max_compression_ratio),
        ):
            if value <= 0:
                raise UnsafeArchiveError(f"{name} must be positive")


@dataclass(frozen=True)
class _ValidatedMember:
    member: tarfile.TarInfo
    relative_path: PurePosixPath


def _safe_root(expected_root: str) -> str:
    if (
        not expected_root
        or expected_root in {".", ".."}
        or "/" in expected_root
        or "\\" in expected_root
        or "\x00" in expected_root
    ):
        raise UnsafeArchiveError(f"unsafe expected archive root: {expected_root!r}")
    return expected_root


def _member_parts(name: str) -> tuple[str, ...]:
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        raise UnsafeArchiveError(f"unsafe archive member path: {name!r}")

    raw_parts = name.split("/")
    if raw_parts[-1] == "":
        raw_parts.pop()
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        raise UnsafeArchiveError(f"unsafe archive member path: {name!r}")

    path = PurePosixPath(*raw_parts)
    if path.is_absolute():
        raise UnsafeArchiveError(f"unsafe archive member path: {name!r}")
    return tuple(raw_parts)


def _validate_members(
    archive: tarfile.TarFile,
    *,
    archive_size: int,
    expected_root: str,
    limits: ArchiveLimits,
) -> list[_ValidatedMember]:
    root = _safe_root(expected_root)
    validated: list[_ValidatedMember] = []
    seen: set[str] = set()
    expanded_bytes = 0

    for member_number, member in enumerate(archive, start=1):
        if member_number > limits.max_members:
            raise UnsafeArchiveError("archive exceeds maximum member count")

        parts = _member_parts(member.name)
        if parts[0] != root:
            raise UnsafeArchiveError(
                f"archive member is outside expected root {root!r}: {member.name!r}"
            )
        if len(parts) == 1 and not member.isdir():
            raise UnsafeArchiveError(f"archive root is not a directory: {member.name!r}")
        if not (member.isdir() or member.isfile()):
            raise UnsafeArchiveError(
                f"archive member is not a regular file or directory: {member.name!r}"
            )

        canonical_name = "/".join(parts)
        if canonical_name in seen:
            raise UnsafeArchiveError(f"archive contains duplicate member: {canonical_name!r}")
        seen.add(canonical_name)

        if member.isfile():
            if member.size < 0:
                raise UnsafeArchiveError(f"archive member has invalid size: {member.name!r}")
            expanded_bytes += member.size
            if expanded_bytes > limits.max_expanded_bytes:
                raise UnsafeArchiveError("archive exceeds maximum expanded size")
            if expanded_bytes > archive_size * limits.max_compression_ratio:
                raise UnsafeArchiveError("archive exceeds maximum compression ratio")

        validated.append(
            _ValidatedMember(member, PurePosixPath(*parts[1:]))
        )

    if not validated:
        # Producers always emit at least the root directory; an empty archive
        # would otherwise silently replace the destination with nothing.
        raise UnsafeArchiveError("archive contains no members")

    ordered = sorted(validated, key=lambda entry: entry.relative_path.parts)
    for current, following in itertools.pairwise(ordered):
        current_parts = current.relative_path.parts
        following_parts = following.relative_path.parts
        if (
            current.member.isfile()
            and len(following_parts) > len(current_parts)
            and following_parts[: len(current_parts)] == current_parts
        ):
            raise UnsafeArchiveError(
                f"archive file is also used as a directory: {current.member.name!r}"
            )

    return validated


def _copy_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    target: Path,
) -> None:
    source = archive.extractfile(member)
    if source is None:
        raise UnsafeArchiveError(f"could not read archive member: {member.name!r}")

    remaining = member.size
    with source, target.open("xb") as destination:
        while remaining:
            chunk = source.read(min(_COPY_CHUNK_SIZE, remaining))
            if not chunk:
                raise UnsafeArchiveError(f"archive member is truncated: {member.name!r}")
            destination.write(chunk)
            remaining -= len(chunk)
    target.chmod(member.mode & 0o777)
    # Keep the archived mtime: export retention and orphan cleanup age files by
    # mtime, so restoring "now" would extend their lifetime by a full window.
    os.utime(target, (member.mtime, member.mtime))


def _extract_to_staging(
    archive: tarfile.TarFile,
    members: list[_ValidatedMember],
    payload_root: Path,
) -> None:
    payload_root.mkdir()
    directory_modes: list[tuple[Path, int]] = []

    for entry in members:
        member = entry.member
        target = payload_root.joinpath(*entry.relative_path.parts)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            directory_modes.append((target, member.mode & 0o777))
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_member(archive, member, target)

    for directory, mode in sorted(
        directory_modes,
        key=lambda item: len(item[0].parts),
        reverse=True,
    ):
        directory.chmod(mode)


def _adopt_destination_owner(payload_root: Path, owner_reference: Path) -> None:
    """When restoring as root, give restored files the destination's owner.

    GNU tar run as root used to preserve the archived uid (the app user); files
    written here would otherwise be root-owned and undeletable by the app.
    """
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    reference = owner_reference.stat()
    for path in [payload_root, *payload_root.rglob("*")]:
        os.lchown(path, reference.st_uid, reference.st_gid)


def _destination_path(destination: Path) -> Path:
    if destination.name in {"", ".", ".."}:
        raise UnsafeArchiveError(f"unsafe restore destination: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = destination.parent.resolve() / destination.name
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise UnsafeArchiveError(f"restore destination is not a regular directory: {target}")
    return target


def _replace_destination(payload_root: Path, destination: Path, staging_root: Path) -> None:
    if not destination.exists():
        os.replace(payload_root, destination)
        return

    previous = staging_root / "previous"
    os.replace(destination, previous)
    try:
        os.replace(payload_root, destination)
    except OSError:
        os.replace(previous, destination)
        raise
    shutil.rmtree(previous)


def _replace_destination_contents(
    payload_root: Path,
    destination: Path,
    staging_root: Path,
) -> None:
    """Replace a mount-point destination without attempting to rename the mount."""
    previous = staging_root / "previous"
    previous.mkdir()
    existing = [child for child in destination.iterdir() if child != staging_root]
    incoming = list(payload_root.iterdir())
    if any(child.name == staging_root.name for child in incoming):
        raise UnsafeArchiveError("archive member conflicts with restore staging")
    moved_existing: list[str] = []
    moved_incoming: list[str] = []

    try:
        for child in existing:
            os.replace(child, previous / child.name)
            moved_existing.append(child.name)
        for child in incoming:
            os.replace(child, destination / child.name)
            moved_incoming.append(child.name)
    except OSError:
        for name in reversed(moved_incoming):
            os.replace(destination / name, payload_root / name)
        for name in reversed(moved_existing):
            os.replace(previous / name, destination / name)
        raise
    shutil.rmtree(previous)


def _requires_content_swap(destination: Path) -> bool:
    if not destination.exists():
        return False
    return (
        os.path.ismount(destination)
        or destination.stat().st_dev != destination.parent.stat().st_dev
    )


def restore_archive(
    archive_path: Path,
    destination: Path,
    *,
    expected_root: str,
    limits: ArchiveLimits | None = None,
) -> None:
    """Validate an archive, then replace only destination with its root contents."""
    effective_limits = limits or ArchiveLimits()
    target = _destination_path(destination)
    archive_size = archive_path.stat().st_size
    use_content_swap = _requires_content_swap(target)
    staging_parent = target if use_content_swap else target.parent

    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = _validate_members(
                archive,
                archive_size=archive_size,
                expected_root=expected_root,
                limits=effective_limits,
            )
            with tempfile.TemporaryDirectory(
                prefix=f".{target.name}.restore-",
                dir=staging_parent,
            ) as temporary:
                staging_root = Path(temporary)
                payload_root = staging_root / "payload"
                _extract_to_staging(archive, members, payload_root)
                _adopt_destination_owner(
                    payload_root, target if target.exists() else target.parent
                )
                if use_content_swap:
                    _replace_destination_contents(payload_root, target, staging_root)
                else:
                    _replace_destination(payload_root, target, staging_root)
    except (tarfile.TarError, EOFError) as exc:
        raise UnsafeArchiveError(f"invalid artifact archive: {archive_path}") from exc


def _environment_limit(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise UnsafeArchiveError(f"{name} must be an integer") from exc


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("expected_root")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    try:
        limits = ArchiveLimits(
            max_members=_environment_limit(
                "RESTORE_ARCHIVE_MAX_MEMBERS",
                DEFAULT_MAX_MEMBERS,
            ),
            max_expanded_bytes=_environment_limit(
                "RESTORE_ARCHIVE_MAX_EXPANDED_BYTES",
                DEFAULT_MAX_EXPANDED_BYTES,
            ),
            max_compression_ratio=_environment_limit(
                "RESTORE_ARCHIVE_MAX_COMPRESSION_RATIO",
                DEFAULT_MAX_COMPRESSION_RATIO,
            ),
        )
        restore_archive(
            args.archive,
            args.destination,
            expected_root=args.expected_root,
            limits=limits,
        )
    except (OSError, UnsafeArchiveError) as exc:
        print(f"[restore] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
