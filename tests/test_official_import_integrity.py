import hashlib
import json

import pytest

from apps.classifications.models import SourceArtifact
from apps.classifications.official_import import (
    FULL_INCHIKEY_RE,
    OfficialImportError,
    import_official_artifact,
)

FULL_INCHIKEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"

# ---------------------------------------------------------------------------
# InChIKey format validation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_official_import_rejects_non_full_inchikey():
    raw = json.dumps([{"inchikey": "XXXXXXXXXXXXXXXXXXXXXXXXXXX"}]).encode()
    with pytest.raises(OfficialImportError):
        import_official_artifact(raw, name="bad.json")


@pytest.mark.django_db
def test_official_import_rejects_14_char_skeleton_only():
    """A 14-char skeleton (no dashes) must be rejected — only 27-char full keys allowed."""
    raw = json.dumps([{"inchikey": "BSYNRYMUTXBXSQ"}]).encode()
    with pytest.raises(OfficialImportError):
        import_official_artifact(raw, name="bad14.json")


@pytest.mark.django_db
def test_official_import_rejects_wrong_segment_lengths():
    """14-10-1 segment pattern must be enforced; wrong lengths are rejected."""
    # 13-10-1 — first segment too short
    raw = json.dumps([{"inchikey": "BSYNRYMUTXBXS-UHFFFAOYSA-N"}]).encode()
    with pytest.raises(OfficialImportError):
        import_official_artifact(raw, name="bad_seg.json")


def test_full_inchikey_re_rejects_lowercase():
    """FULL_INCHIKEY_RE must only accept uppercase A-Z letters."""
    assert FULL_INCHIKEY_RE.fullmatch("bsynrymutxbxsq-uhfffaoysa-n") is None


def test_full_inchikey_re_accepts_valid_key():
    assert FULL_INCHIKEY_RE.fullmatch(FULL_INCHIKEY) is not None


# ---------------------------------------------------------------------------
# Source-artifact byte integrity through storage
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_official_import_stores_source_bytes():
    raw = json.dumps(
        [{"inchikey": FULL_INCHIKEY, "raw": {"ok": True}}]
    ).encode()

    result = import_official_artifact(raw, name="official.json")
    artifact = SourceArtifact.objects.get(pk=result.artifact_id)

    assert artifact.storage_key
    assert artifact.byte_size == len(raw)
    assert artifact.checksum


@pytest.mark.django_db
def test_official_import_storage_key_and_content_sha256_match():
    """storage_key is derived from the sha256 digest and content_sha256 mirrors checksum."""
    raw = json.dumps(
        [{"inchikey": FULL_INCHIKEY, "raw": {"integrity": True}}]
    ).encode()
    expected_digest = hashlib.sha256(raw).hexdigest()

    result = import_official_artifact(raw, name="integrity_check.json")
    artifact = SourceArtifact.objects.get(pk=result.artifact_id)

    # storage_key is "{checksum}.json" — the checksum prefix must match.
    assert artifact.storage_key == f"{expected_digest}.json", (
        f"storage_key mismatch: expected {expected_digest}.json, got {artifact.storage_key}"
    )
    # content_sha256 mirrors checksum on official import.
    assert artifact.content_sha256 == expected_digest, (
        f"content_sha256 mismatch: expected {expected_digest}, got {artifact.content_sha256}"
    )
    # OfficialImportResult.checksum reflects the same digest.
    assert result.checksum == expected_digest
    assert result.byte_size == len(raw)


@pytest.mark.django_db
def test_official_import_stored_bytes_match_input(tmp_path, settings):
    """Bytes written to SOURCE_ARTIFACT_DIR must be identical to the input raw bytes."""
    settings.SOURCE_ARTIFACT_DIR = str(tmp_path)

    raw = json.dumps(
        [{"inchikey": FULL_INCHIKEY, "raw": {"round_trip": True}}]
    ).encode()
    expected_digest = hashlib.sha256(raw).hexdigest()

    result = import_official_artifact(raw, name="round_trip.json")
    artifact = SourceArtifact.objects.get(pk=result.artifact_id)

    # Retrieve the bytes directly from the storage path and verify they are identical.
    stored_path = tmp_path / artifact.storage_key
    assert stored_path.exists(), f"Stored artifact not found at {stored_path}"
    stored_bytes = stored_path.read_bytes()
    assert stored_bytes == raw, "Stored source-artifact bytes differ from input"
    assert hashlib.sha256(stored_bytes).hexdigest() == expected_digest
