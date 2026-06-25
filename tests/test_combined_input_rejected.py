"""Reject an ambiguous submission that provides BOTH pasted text and a file.

Previously a POST carrying a non-empty ``text_input`` AND an ``input_file``
spooled only the file and silently dropped the pasted text. Both the JSON API
route (``/api/jobs``) and the HTML submit route (``/``) must now reject the
combined upload with a 400 and create no BatchJob.
"""
from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.batches.models import BatchJob


@pytest.mark.django_db
def test_api_rejects_combined_text_and_file(client, settings, tmp_path):
    settings.INGEST_DEFER_HEAVY_PROCESSING = True
    settings.UPLOAD_ARTIFACT_DIR = str(tmp_path / "uploads")

    upload = SimpleUploadedFile(
        "input.csv",
        b"id,smiles\nrow1,CCO\n",
        content_type="text/csv",
    )
    response = client.post(
        "/api/jobs",
        data={"input_file": upload, "text_input": "CCO\n", "remote_enabled": "true"},
        HTTP_AUTHORIZATION="Bearer dev-token",
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "Provide either pasted text or a file upload, not both."
    }
    assert BatchJob.objects.count() == 0


@pytest.mark.django_db
def test_html_submit_rejects_combined_text_and_file(client, settings, tmp_path):
    settings.INGEST_DEFER_HEAVY_PROCESSING = True
    settings.UPLOAD_ARTIFACT_DIR = str(tmp_path / "uploads")

    upload = SimpleUploadedFile(
        "input.csv",
        b"id,smiles\nrow1,CCO\n",
        content_type="text/csv",
    )
    response = client.post(
        "/",
        data={"input_file": upload, "text_input": "CCO\n", "remote_enabled": "on"},
    )

    assert response.status_code == 400
    assert BatchJob.objects.count() == 0
