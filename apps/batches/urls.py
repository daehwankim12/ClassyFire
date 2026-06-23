"""URL patterns for the batches API and web UI."""
from django.urls import path

from apps.batches.api import (
    create_export_view,
    create_job_view,
    download_export_view,
    item_structure_view,
    job_items_view,
    job_status_view,
)

app_name = "batches"

urlpatterns = [
    # Existing API
    path("jobs", create_job_view, name="create_job"),
    # New API endpoints (Task 8)
    path("jobs/<str:job_id>", job_status_view, name="job_status"),
    path("jobs/<str:job_id>/items", job_items_view, name="job_items"),
    path("jobs/<str:job_id>/exports", create_export_view, name="create_export"),
    path("exports/<str:token>", download_export_view, name="download_export"),
    path("items/<int:item_id>/structure", item_structure_view, name="item_structure"),
]
