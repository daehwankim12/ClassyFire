import json

import pytest

from apps.classifications.models import SourceArtifact
from apps.classifications.official_import import OfficialImportError, import_official_artifact


@pytest.mark.django_db
def test_official_import_rejects_non_full_inchikey():
    raw = json.dumps([{"inchikey": "XXXXXXXXXXXXXXXXXXXXXXXXXXX"}]).encode()
    with pytest.raises(OfficialImportError):
        import_official_artifact(raw, name="bad.json")


@pytest.mark.django_db
def test_official_import_stores_source_bytes():
    raw = json.dumps(
        [{"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N", "raw": {"ok": True}}]
    ).encode()

    result = import_official_artifact(raw, name="official.json")
    artifact = SourceArtifact.objects.get(pk=result.artifact_id)

    assert artifact.storage_key
    assert artifact.byte_size == len(raw)
    assert artifact.checksum
