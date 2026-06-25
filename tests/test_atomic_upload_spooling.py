from __future__ import annotations

import pytest

from apps.batches.ingest import UploadTooLarge, cleanup_stale_upload_temps, spool_upload_file


class ChunkedUpload:
    name = "input.csv"
    content_type = "text/csv"

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def chunks(self):
        yield from self._chunks


def test_spool_upload_writes_tmp_then_atomic_final(settings, tmp_path):
    settings.UPLOAD_ARTIFACT_DIR = str(tmp_path)
    upload = ChunkedUpload([b"id,smiles\n", b"row1,CCO\n"])
    result = spool_upload_file(upload, max_bytes=1024)
    final = tmp_path / result.relative_path
    assert final.exists()
    assert final.read_bytes() == b"id,smiles\nrow1,CCO\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_spool_upload_enforces_size_during_stream(settings, tmp_path):
    settings.UPLOAD_ARTIFACT_DIR = str(tmp_path)
    upload = ChunkedUpload([b"a" * 10, b"b" * 10])
    with pytest.raises(UploadTooLarge):
        spool_upload_file(upload, max_bytes=15)
    assert not list(tmp_path.glob("*"))


def test_cleanup_stale_upload_temps(settings, tmp_path):
    settings.UPLOAD_ARTIFACT_DIR = str(tmp_path)
    tmp_file = tmp_path / "stale.tmp"
    tmp_file.write_bytes(b"partial")
    removed = cleanup_stale_upload_temps(max_age_seconds=0)
    assert removed == 1
    assert not tmp_file.exists()
