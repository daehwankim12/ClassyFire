"""Artifact storage seam for generated batch export files."""
from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


@dataclass(frozen=True)
class AtomicWriteHandle:
    """Open temporary file handle used before atomic promotion to final path."""

    path: Path
    file: BinaryIO


@dataclass(frozen=True)
class ArtifactStat:
    """Checksum-compatible storage stat for export artifact metadata."""

    byte_size: int
    checksum: str


class ArtifactStorage(Protocol):
    """Storage operations needed by export artifact generation and downloads."""

    def open_atomic_write(self, token: str) -> AbstractContextManager[AtomicWriteHandle]:
        """Open a temporary file for writing an export artifact."""

    def commit_atomic_write(self, temp_path: Path, token: str, fmt: str) -> Path:
        """Atomically promote a temporary artifact into its final location."""

    def local_path(self, token: str, fmt: str) -> Path:
        """Return a local download path for a stored artifact."""

    def meta_path(self, token: str) -> Path:
        """Return a local path for the legacy sidecar metadata."""

    def open(self, path: Path, mode: str = "rb") -> BinaryIO:
        """Open an artifact path for reading."""

    def exists(self, path: Path) -> bool:
        """Return True if the storage path exists."""

    def delete(self, path: Path) -> bool:
        """Delete a storage path if present and return whether it was removed."""

    def stat(self, path: Path) -> ArtifactStat:
        """Return byte size and checksum for a storage path."""

    def checksum(self, path: Path) -> str:
        """Return the SHA-256 hex digest for a storage path."""

    def write_bytes_atomic(self, path: Path, content: bytes) -> None:
        """Atomically write small storage metadata bytes."""


class LocalArtifactStorage:
    """Local filesystem implementation for export artifacts."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @contextmanager
    def open_atomic_write(self, token: str) -> Iterator[AtomicWriteHandle]:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp_path_str = tempfile.mkstemp(dir=self.root, prefix=f".tmp.{token}.")
        tmp_path = Path(tmp_path_str)
        file_obj = os.fdopen(fd, "wb")
        try:
            try:
                yield AtomicWriteHandle(path=tmp_path, file=file_obj)
            finally:
                if not file_obj.closed:
                    file_obj.close()
        except BaseException:
            self.delete(tmp_path)
            raise

    def commit_atomic_write(self, temp_path: Path, token: str, fmt: str) -> Path:
        dest = self.local_path(token, fmt)
        self.root.mkdir(parents=True, exist_ok=True)
        os.replace(temp_path, dest)
        return dest

    def local_path(self, token: str, fmt: str) -> Path:
        return self.root / f"{token}.{fmt}"

    def meta_path(self, token: str) -> Path:
        return self.root / f"{token}.meta.json"

    def open(self, path: Path, mode: str = "rb") -> BinaryIO:
        return cast(BinaryIO, path.open(mode))

    def exists(self, path: Path) -> bool:
        return path.exists()

    def delete(self, path: Path) -> bool:
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def stat(self, path: Path) -> ArtifactStat:
        return ArtifactStat(byte_size=path.stat().st_size, checksum=self.checksum(path))

    def checksum(self, path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def write_bytes_atomic(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path_str = tempfile.mkstemp(dir=path.parent, prefix=f".tmp.{path.name}.")
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            os.replace(tmp_path, path)
        except BaseException:
            self.delete(tmp_path)
            raise


def get_artifact_storage(root: str | Path | None = None) -> ArtifactStorage:
    """Return the configured artifact storage backend.

    Only the local filesystem backend is implemented. Unsupported values fail
    closed so a typo or future S3 setting cannot silently write to local disk.
    """
    backend = str(
        getattr(settings, "EXPORT_ARTIFACT_STORAGE_BACKEND", "local")
    ).strip().lower() or "local"
    if backend != "local":
        raise ImproperlyConfigured(
            f"Export artifact storage backend {backend!r} is not implemented. "
            "Set EXPORT_ARTIFACT_STORAGE_BACKEND='local' to use local filesystem storage."
        )

    storage_root = Path(
        root if root is not None else getattr(settings, "EXPORT_DIR", settings.BASE_DIR / "export_cache")
    )
    return LocalArtifactStorage(storage_root)


__all__ = [
    "ArtifactStat",
    "ArtifactStorage",
    "AtomicWriteHandle",
    "LocalArtifactStorage",
    "get_artifact_storage",
]
