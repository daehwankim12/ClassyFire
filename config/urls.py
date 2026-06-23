"""Root URL configuration."""
from django.contrib import admin
from django.urls import include, path

from apps.batches.views import (
    item_table_partial_view,
    job_detail_view,
    stats_view,
    structure_form_view,
    submit_view,
)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("apps.batches.urls")),
    # Web UI routes
    path("", submit_view, name="submit"),
    path("jobs/<str:job_id>/<str:secret>/", job_detail_view, name="job_detail"),
    path("jobs/<str:job_id>/<str:secret>/items", item_table_partial_view, name="item_table_partial"),
    path("items/<int:item_id>/<str:secret>/structure", structure_form_view, name="structure_form"),
    path("stats/", stats_view, name="stats"),
]
