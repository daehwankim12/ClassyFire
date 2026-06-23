"""Operational tables: seed imports and the system event log."""
from __future__ import annotations

from django.db import models

from apps.core.enums import EventCategory


class SeedImport(models.Model):
    """An audited import of a seed dataset (DOI-cited, checksummed, licensed)."""

    doi = models.TextField(null=True, blank=True)
    record_id = models.TextField(null=True, blank=True, help_text="Source record id.")
    filename = models.TextField(null=True, blank=True)
    checksum = models.TextField(null=True, blank=True, help_text="File checksum.")
    license = models.TextField(null=True, blank=True)
    expected_row_count = models.IntegerField(null=True, blank=True)
    actual_row_count = models.IntegerField(null=True, blank=True)
    duplicate_count = models.IntegerField(null=True, blank=True)
    audit_result = models.JSONField(
        null=True, blank=True, help_text="Structured audit findings as JSONB."
    )
    import_status = models.TextField(
        default="PENDING", help_text="Coarse import lifecycle status."
    )
    activated_table = models.TextField(
        null=True, blank=True, help_text="Physical seed table this import activated."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    who = models.TextField(null=True, blank=True, help_text="Actor who ran the import.")

    class Meta:
        db_table = "seed_import"

    def __str__(self) -> str:
        return f"seed_import:{self.pk} ({self.import_status})"


class SystemEvent(models.Model):
    """Append-only structured event log (worker/remote/backup/seed/security)."""

    category = models.CharField(max_length=16, choices=EventCategory.choices)
    event_type = models.TextField(help_text="Specific event type within the category.")
    payload = models.JSONField(default=dict, blank=True, help_text="Event detail as JSONB.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "system_event"
        indexes = [
            models.Index(fields=["category", "created_at"], name="system_event_cat_idx"),
        ]

    def __str__(self) -> str:
        return f"event:{self.category}/{self.event_type}"
