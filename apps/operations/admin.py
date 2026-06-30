"""Admin registration for operational tables (all read-only).

``SystemEvent`` and ``AuditSignature`` form the signed, append-only audit log;
they are strictly read-only so the chain of custody is never altered through
the admin.
"""
from __future__ import annotations

from django.contrib import admin

from apps.core.admin_base import ReadOnlyModelAdmin
from apps.operations.models import AuditSignature, SystemEvent


@admin.register(SystemEvent)
class SystemEventAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "category", "event_type", "created_at")
    list_filter = ("category",)
    search_fields = ("event_type",)


@admin.register(AuditSignature)
class AuditSignatureAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "event", "key_id", "payload_sha256", "created_at")
    search_fields = ("payload_sha256", "signature")
    list_select_related = ("event",)
