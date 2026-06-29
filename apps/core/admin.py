"""Admin registration for core infrastructure models (read-only)."""
from __future__ import annotations

from django.contrib import admin

from apps.core.admin_base import ReadOnlyModelAdmin
from apps.core.models import LoginThrottle


@admin.register(LoginThrottle)
class LoginThrottleAdmin(ReadOnlyModelAdmin):
    list_display = ("key", "failure_count", "window_started_at", "updated_at")
    search_fields = ("key",)
