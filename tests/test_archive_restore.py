from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import pytest

from apps.operations.archive_restore import (
    ArchiveLimits,
    UnsafeArchiveError,
    restore_archive,
)


def _add_directory(archive: tarfile.TarFile, name: str) -> None:
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE
    member.mode = 0o750
    archive.addfile(member)


def _add_file(archive: tarfile.TarFile, name: str, content: bytes = b"payload") -> None:
    member = tarfile.TarInfo(name)
    member.size = len(content)
    member.mode = 0o640
    archive.addfile(member, io.BytesIO(content))


def _regular_archive(path: Path, root: str, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        _add_directory(archive, root)
        for relative_name, content in files.items():
            _add_file(archive, f"{root}/{relative_name}", content)


def test_restore_archive_replaces_only_configured_destination(tmp_path: Path):
    archive_path = tmp_path / "exports.tar.gz"
    _regular_archive(
        archive_path,
        "export_files",
        {"nested/report.csv": b"compound,class\naspirin,benzenoid\n"},
    )
    destination = tmp_path / "restored-exports"
    destination.mkdir()
    (destination / "stale.csv").write_text("stale")
    sibling = tmp_path / "application.py"
    sibling.write_text("preserved")

    restore_archive(archive_path, destination, expected_root="export_files")

    assert (destination / "nested" / "report.csv").read_bytes().startswith(b"compound")
    assert not (destination / "stale.csv").exists()
    assert sibling.read_text() == "preserved"
    assert not (tmp_path / "export_files").exists()


def test_restore_archive_preserves_mount_point_directory(tmp_path: Path, monkeypatch):
    archive_path = tmp_path / "uploads.tar.gz"
    _regular_archive(archive_path, "uploads", {"new.csv": b"new"})
    destination = tmp_path / "uploads"
    destination.mkdir()
    (destination / "old.csv").write_text("old")
    original_inode = destination.stat().st_ino
    real_ismount = os.path.ismount
    monkeypatch.setattr(
        "apps.operations.archive_restore.os.path.ismount",
        lambda path: Path(path) == destination or real_ismount(path),
    )

    restore_archive(archive_path, destination, expected_root="uploads")

    assert destination.stat().st_ino == original_inode
    assert (destination / "new.csv").read_bytes() == b"new"
    assert not (destination / "old.csv").exists()


def test_restore_archive_rejects_sibling_top_level_root(tmp_path: Path):
    archive_path = tmp_path / "wrong-root.tar.gz"
    _regular_archive(archive_path, "templates", {"base.html": b"owned"})
    destination = tmp_path / "export_files"
    destination.mkdir()
    marker = destination / "existing.csv"
    marker.write_text("preserved")

    with pytest.raises(UnsafeArchiveError, match="outside expected root"):
        restore_archive(archive_path, destination, expected_root="export_files")

    assert marker.read_text() == "preserved"
    assert not (tmp_path / "templates").exists()


@pytest.mark.parametrize(
    "member_name",
    [
        "/export_files/absolute.csv",
        "export_files/../outside.csv",
        "export_files\\..\\outside.csv",
        "export_files//outside.csv",
    ],
)
def test_restore_archive_rejects_unsafe_member_paths(
    tmp_path: Path,
    member_name: str,
):
    archive_path = tmp_path / "unsafe-path.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        _add_file(archive, member_name)

    with pytest.raises(UnsafeArchiveError, match="unsafe archive member path"):
        restore_archive(
            archive_path,
            tmp_path / "export_files",
            expected_root="export_files",
        )


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_restore_archive_rejects_links_and_special_files(
    tmp_path: Path,
    member_type: bytes,
):
    archive_path = tmp_path / "unsafe-type.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo("export_files/unsafe")
        member.type = member_type
        member.linkname = "../outside"
        archive.addfile(member)

    with pytest.raises(UnsafeArchiveError, match="not a regular file or directory"):
        restore_archive(
            archive_path,
            tmp_path / "export_files",
            expected_root="export_files",
        )


def test_restore_archive_rejects_duplicate_members(tmp_path: Path):
    archive_path = tmp_path / "duplicate.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        _add_file(archive, "export_files/report.csv", b"first")
        _add_file(archive, "export_files/report.csv", b"second")

    with pytest.raises(UnsafeArchiveError, match="duplicate member"):
        restore_archive(
            archive_path,
            tmp_path / "export_files",
            expected_root="export_files",
        )


def test_restore_archive_enforces_member_limit(tmp_path: Path):
    archive_path = tmp_path / "too-many.tar.gz"
    _regular_archive(archive_path, "uploads", {"one": b"1", "two": b"2"})

    with pytest.raises(UnsafeArchiveError, match="maximum member count"):
        restore_archive(
            archive_path,
            tmp_path / "uploads",
            expected_root="uploads",
            limits=ArchiveLimits(max_members=2),
        )


def test_restore_archive_enforces_expanded_size_limit(tmp_path: Path):
    archive_path = tmp_path / "too-large.tar.gz"
    _regular_archive(archive_path, "media", {"large.bin": b"1234"})

    with pytest.raises(UnsafeArchiveError, match="maximum expanded size"):
        restore_archive(
            archive_path,
            tmp_path / "media",
            expected_root="media",
            limits=ArchiveLimits(max_expanded_bytes=3),
        )


def test_restore_archive_enforces_compression_ratio_limit(tmp_path: Path):
    archive_path = tmp_path / "compressed.tar.gz"
    _regular_archive(archive_path, "media", {"zeros.bin": b"0" * 100_000})

    with pytest.raises(UnsafeArchiveError, match="maximum compression ratio"):
        restore_archive(
            archive_path,
            tmp_path / "media",
            expected_root="media",
            limits=ArchiveLimits(max_compression_ratio=2),
        )


def test_restore_script_routes_every_artifact_through_safe_extractor():
    script = Path("deploy/backup/restore.sh").read_text()

    assert "tar -xzf" not in script
    assert script.count("_safe_extract_archive \\\n") == 4
    assert '"${RESTORE_MEDIA_ARCHIVE_ROOT}"' in script
    assert '"${RESTORE_EXPORT_ARCHIVE_ROOT}"' in script
    assert '"${RESTORE_SOURCE_ARTIFACT_ARCHIVE_ROOT}"' in script
    assert '"${RESTORE_UPLOAD_ARCHIVE_ROOT}"' in script


def test_restore_archive_rejects_empty_archive_without_touching_destination(
    tmp_path: Path,
):
    archive_path = tmp_path / "media.tar.gz"
    with tarfile.open(archive_path, "w:gz"):
        pass
    destination = tmp_path / "media"
    destination.mkdir()
    (destination / "important.png").write_bytes(b"keep")

    with pytest.raises(UnsafeArchiveError, match="no members"):
        restore_archive(archive_path, destination, expected_root="media")

    assert (destination / "important.png").read_bytes() == b"keep"


def test_restore_archive_preserves_member_mtime(tmp_path: Path):
    archive_path = tmp_path / "exports.tar.gz"
    archived_mtime = 1_700_000_000
    with tarfile.open(archive_path, "w:gz") as archive:
        _add_directory(archive, "export_files")
        member = tarfile.TarInfo("export_files/old.csv")
        member.size = 3
        member.mode = 0o640
        member.mtime = archived_mtime
        archive.addfile(member, io.BytesIO(b"a,b"))
    destination = tmp_path / "restored"

    restore_archive(archive_path, destination, expected_root="export_files")

    assert int((destination / "old.csv").stat().st_mtime) == archived_mtime


def test_restore_script_preflights_extractor_before_pg_restore():
    script = (Path(__file__).resolve().parents[1] / "deploy/backup/restore.sh").read_text()

    preflight = script.index('-c "import apps.operations.archive_restore"')
    assert preflight < script.index("\npg_restore \\")


def test_restore_smoke_names_media_archive_root_after_its_source_dir():
    smoke = (
        Path(__file__).resolve().parents[1] / "scripts/run_backup_restore_smoke.sh"
    ).read_text()

    assert 'MEDIA_DIR="${UPLOAD_DIR}"' in smoke
    assert 'RESTORE_MEDIA_ARCHIVE_ROOT="$(basename "${UPLOAD_DIR}")"' in smoke
