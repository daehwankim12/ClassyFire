import pytest

from apps.batches.models import BatchItem, BatchJob
from apps.batches.services import run_pending_jobs
from apps.core.enums import BatchItemStatus


@pytest.mark.django_db
def test_overlong_text_row_is_invalid_without_rdkit_parse(client, monkeypatch, settings):
    settings.INGEST_DEFER_HEAVY_PROCESSING = True

    def fail_compute_identifiers(raw, detected_type):
        raise AssertionError("overlong row must not reach RDKit")

    monkeypatch.setattr(
        "apps.compounds.validators.compute_identifiers",
        fail_compute_identifiers,
    )

    long_row = "C" * 100_017
    response = client.post(
        "/api/jobs",
        data={"text_input": long_row, "remote_enabled": False},
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer dev-token",
    )

    assert response.status_code == 202
    job = BatchJob.objects.get(pk=response.json()["job_id"])
    run_pending_jobs("test-worker", max_jobs=1)
    item = BatchItem.objects.get(job=job)
    assert item.status == BatchItemStatus.INVALID_INPUT
    assert "Row exceeds maximum length" in item.message
    assert "100017" in item.message
    assert "[truncated, 100017 chars total]" in item.original_input
