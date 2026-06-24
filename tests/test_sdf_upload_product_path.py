import io

import pytest

from apps.batches.models import BatchItem, BatchJob
from apps.batches.services import run_pending_jobs
from apps.core.enums import BatchItemStatus, DetectedInputType

ETHANOL_SDF = """
  Mrv2014 09152006042D

  3  2  0  0  0  0            999 V2000
   -0.4125    0.7145    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
    0.4125    0.7145    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    0.8250    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0  0  0  0
  2  3  1  0  0  0  0
M  END
> <EXTERNAL_ID>
ethanol-1

$$$$
"""


@pytest.mark.django_db
def test_api_sdf_upload_is_normalized_as_sdf(client, settings):
    settings.INGEST_DEFER_HEAVY_PROCESSING = True
    upload = io.BytesIO(ETHANOL_SDF.encode("utf-8"))
    upload.name = "ethanol.sdf"

    response = client.post(
        "/api/jobs",
        data={"input_file": upload, "remote_enabled": "false"},
        HTTP_AUTHORIZATION="Bearer dev-token",
    )

    assert response.status_code == 202
    job = BatchJob.objects.get(pk=response.json()["job_id"])
    assert run_pending_jobs("test-worker", max_jobs=1) == 1
    item = BatchItem.objects.get(job=job)
    assert item.detected_type == DetectedInputType.SDF
    assert item.external_id == "ethanol-1"
    assert item.inchikey is not None
    assert item.status in {
        BatchItemStatus.FOUND_LIVE_CACHE,
        BatchItemStatus.FOUND_SEED_CACHE,
        BatchItemStatus.REMOTE_DELAYED,
    }


@pytest.mark.django_db
def test_blank_title_sdf_preserves_molblock_line_positions(client, settings):
    settings.INGEST_DEFER_HEAVY_PROCESSING = True
    upload = io.BytesIO(ETHANOL_SDF.encode("utf-8"))
    upload.name = "blank-title.sdf"
    response = client.post(
        "/api/jobs",
        data={"input_file": upload, "remote_enabled": "false"},
        HTTP_AUTHORIZATION="Bearer dev-token",
    )
    assert response.status_code == 202
    job = BatchJob.objects.get(pk=response.json()["job_id"])
    run_pending_jobs("test-worker", max_jobs=1)
    item = BatchItem.objects.get(job=job)
    assert item.detected_type == DetectedInputType.SDF
    assert item.inchikey is not None
