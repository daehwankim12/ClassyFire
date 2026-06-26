"""Root URL configuration."""
from django.contrib import admin
from django.urls import include, path

from apps.batches.views import (
    item_table_partial_view,
    job_claim_view,
    job_detail_view,
    job_progress_partial_view,
    stats_view,
    structure_form_view,
    submit_view,
)
from apps.core.views import health, shared_access_view

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health, name="health"),
    path("access/", shared_access_view, name="shared_access"),
    path("api/", include("apps.batches.urls")),
    # Web UI routes
    path("", submit_view, name="submit"),
    path("jobs/<str:job_id>/claim/<str:secret>/", job_claim_view, name="job_claim"),
    path("jobs/<str:job_id>/", job_detail_view, name="job_detail_secretless"),
    path("jobs/<str:job_id>/items", item_table_partial_view, name="item_table_partial_secretless"),
    path("jobs/<str:job_id>/progress", job_progress_partial_view, name="job_progress_secretless"),
    path("jobs/<str:job_id>/<str:secret>/", job_detail_view, name="job_detail"),
    path("jobs/<str:job_id>/<str:secret>/items", item_table_partial_view, name="item_table_partial"),
    path("jobs/<str:job_id>/<str:secret>/progress", job_progress_partial_view, name="job_progress"),
    path("items/<int:item_id>/structure", structure_form_view, name="structure_form_secretless"),
    path("items/<int:item_id>/<str:secret>/structure", structure_form_view, name="structure_form"),
    path("stats/", stats_view, name="stats"),
]
