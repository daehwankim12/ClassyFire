"""URL patterns for the batches API."""
from django.urls import path

from apps.batches.api import create_job_view

app_name = "batches"

urlpatterns = [
    path("jobs", create_job_view, name="create_job"),
]
