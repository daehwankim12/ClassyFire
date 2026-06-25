from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, cast

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import Client

from apps.batches.ingest import parse_text_input
from apps.batches.services import create_job

if TYPE_CHECKING:
    from apps.batches.storage import ArtifactStat, AtomicWriteHandle

ASPIRIN_SMILES = "CC(=O)Oc1ccccc1C(=O)O"


def _create_simple_job():
    rows = list(parse_text_input(ASPIRIN_SMILES))
    return create_job(rows, remote_enabled=False)


def test_local_artifact_storage_save_bytes_roundtrip(tmp_path: Path):
    """save_bytes writes data atomically; the stored bytes are byte-for-byte identical."""
    from apps.batches.storage import LocalArtifactStorage

    storage = LocalArtifactStorage(tmp_path)
    payload = b"source artifact content\n"
    key = "abc123def456.json"

    stored_path = storage.save_bytes(key, payload)

    assert stored_path == tmp_path / key
    assert storage.exists(stored_path)
    with storage.open(stored_path, "rb") as fh:
        assert fh.read() == payload
    assert storage.checksum(stored_path) == hashlib.sha256(payload).hexdigest()


def test_local_artifact_storage_atomic_write_stat_open_and_delete(tmp_path: Path):
    from apps.batches.storage import LocalArtifactStorage

    payload = b"artifact bytes\n"
    storage = LocalArtifactStorage(tmp_path)

    with storage.open_atomic_write("abc123") as writer:
        writer.file.write(payload)

    temp_path = writer.path
    assert storage.exists(temp_path)

    final_path = storage.commit_atomic_write(temp_path, "token123", "csv")

    assert final_path == tmp_path / "token123.csv"
    assert not temp_path.exists()
    assert storage.exists(final_path)
    assert storage.local_path("token123", "csv") == final_path

    artifact_stat = storage.stat(final_path)
    assert artifact_stat.byte_size == len(payload)
    assert artifact_stat.checksum == hashlib.sha256(payload).hexdigest()
    assert storage.checksum(final_path) == artifact_stat.checksum

    with storage.open(final_path, "rb") as fh:
        assert fh.read() == payload

    assert storage.delete(final_path) is True
    assert storage.exists(final_path) is False
    assert storage.delete(final_path) is False


class RecordingStorage:
    def __init__(self, delegate: object) -> None:
        from apps.batches.storage import ArtifactStorage

        self.delegate = cast(ArtifactStorage, delegate)
        self.calls: list[str] = []

    def _record(self, name: str) -> None:
        self.calls.append(name)

    def open_atomic_write(self, token: str) -> AbstractContextManager[AtomicWriteHandle]:
        self._record("open_atomic_write")
        return self.delegate.open_atomic_write(token)

    def commit_atomic_write(self, temp_path: Path, token: str, fmt: str) -> Path:
        self._record("commit_atomic_write")
        return self.delegate.commit_atomic_write(temp_path, token, fmt)

    def local_path(self, token: str, fmt: str) -> Path:
        self._record("local_path")
        return self.delegate.local_path(token, fmt)

    def meta_path(self, token: str) -> Path:
        self._record("meta_path")
        return self.delegate.meta_path(token)

    def open(self, path: Path, mode: str = "rb") -> BinaryIO:
        self._record("open")
        return self.delegate.open(path, mode)

    def exists(self, path: Path) -> bool:
        self._record("exists")
        return self.delegate.exists(path)

    def delete(self, path: Path) -> bool:
        self._record("delete")
        return self.delegate.delete(path)

    def stat(self, path: Path) -> ArtifactStat:
        self._record("stat")
        return self.delegate.stat(path)

    def checksum(self, path: Path) -> str:
        self._record("checksum")
        return self.delegate.checksum(path)

    def write_bytes_atomic(self, path: Path, content: bytes) -> None:
        self._record("write_bytes_atomic")
        self.delegate.write_bytes_atomic(path, content)


@pytest.mark.django_db(transaction=True)
def test_exports_save_and_validate_download_path_through_artifact_storage(
    monkeypatch,
    tmp_path: Path,
):
    from apps.batches import exports
    from apps.batches.models import ExportArtifact
    from apps.batches.storage import LocalArtifactStorage

    storage = RecordingStorage(LocalArtifactStorage(tmp_path))
    monkeypatch.setattr(exports, "_artifact_storage", lambda: storage)

    job, secret = _create_simple_job()
    client = Client()

    create_response = client.post(
        f"/api/jobs/{job.pk}/exports",
        data=json.dumps({"format": "csv", "secret": secret}),
        content_type="application/json",
    )

    assert create_response.status_code == 201
    token = create_response.json()["token"]
    artifact = ExportArtifact.objects.get(token=token)
    artifact_path = Path(artifact.path)

    assert artifact_path == tmp_path / f"{token}.csv"
    assert storage.exists(artifact_path)

    with storage.open(artifact_path, "rb") as fh:
        payload = fh.read()
    assert artifact.byte_size == len(payload)
    assert artifact.checksum == hashlib.sha256(payload).hexdigest()

    download_path = exports.get_export_path(token)
    assert download_path == artifact_path

    download_response = client.get(f"/api/exports/{token}")
    assert download_response.status_code == 200
    response_with_stream = cast(Any, download_response)
    streaming_content = cast(Iterable[bytes], response_with_stream.streaming_content)
    assert b"InChIKey" in b"".join(streaming_content)

    assert "open_atomic_write" in storage.calls
    assert "commit_atomic_write" in storage.calls
    assert "checksum" in storage.calls
    assert "exists" in storage.calls
    assert "open" in storage.calls


@pytest.mark.django_db(transaction=True)
def test_unsupported_artifact_storage_backend_fails_closed(
    settings,
    monkeypatch,
    tmp_path: Path,
):
    from apps.batches import exports

    job, _secret = _create_simple_job()
    settings.EXPORT_ARTIFACT_STORAGE_BACKEND = "s3"
    monkeypatch.setattr(exports, "EXPORT_DIR", tmp_path)

    with pytest.raises(ImproperlyConfigured, match=r"s3.*not implemented"):
        exports.create_export_for_authorized_job(job, "csv")

    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []
