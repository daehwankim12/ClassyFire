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
        BatchItemStatus.REMOTE_DELAYED,
    }


ETHANOL_TITLED = """ethanol
  CDK     0922211524

  3  2  0  0  0  0            999 V2000
    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.2990    0.7500    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    2.5981    0.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0  0  0  0
  2  3  1  0  0  0  0
M  END
$$$$
"""

ETHANOL_BLANK_TITLE = """
  CDK     0922211524

  3  2  0  0  0  0            999 V2000
    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.2990    0.7500    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    2.5981    0.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0  0  0  0
  2  3  1  0  0  0  0
M  END
$$$$
"""

MALFORMED_RECORD = """broken
  CDK

this is not a molfile
$$$$
"""


@pytest.mark.django_db
def test_sdf_upload_titled_blank_and_malformed_middle_records(client, settings):
    settings.INGEST_DEFER_HEAVY_PROCESSING = True
    payload = (ETHANOL_TITLED + MALFORMED_RECORD + ETHANOL_BLANK_TITLE).encode()
    upload = io.BytesIO(payload)
    upload.name = "records.sdf"

    response = client.post(
        "/api/jobs",
        data={"input_file": upload, "remote_enabled": "false"},
        HTTP_AUTHORIZATION="Bearer dev-token",
    )
    assert response.status_code == 202

    run_pending_jobs("test-worker", max_jobs=1)

    job = BatchJob.objects.get(pk=response.json()["job_id"])
    rows = list(BatchItem.objects.filter(job=job).order_by("row_order"))
    assert [row.row_order for row in rows] == [0, 1, 2]
    assert rows[0].submitted_type == DetectedInputType.SDF
    assert rows[0].detected_type == DetectedInputType.SDF
    assert rows[0].inchikey == "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
    assert rows[0].status != BatchItemStatus.INVALID_INPUT
    assert rows[1].submitted_type == DetectedInputType.SDF
    assert rows[1].status == BatchItemStatus.INVALID_INPUT
    assert rows[2].submitted_type == DetectedInputType.SDF
    assert rows[2].detected_type == DetectedInputType.SDF
    assert rows[2].inchikey == "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
    assert rows[2].status != BatchItemStatus.INVALID_INPUT


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
