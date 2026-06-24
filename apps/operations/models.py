"""Operational tables: seed imports and the system event log."""
from __future__ import annotations

from typing import Any

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
        db_table = "seed_imports"

    def __str__(self) -> str:
        return f"seed_import:{self.pk} ({self.import_status})"


class SystemEventManager(models.Manager["SystemEvent"]):
    """Manager that signs every event created through the normal ORM path."""

    def create(self, **kwargs: Any) -> SystemEvent:
        event = super().create(**kwargs)
        from apps.operations.audit import sign_system_event

        sign_system_event(event)
        return event


class SystemEvent(models.Model):
    """Append-only structured event log (worker/remote/backup/seed/security)."""

    category = models.CharField(max_length=16, choices=EventCategory.choices)
    event_type = models.TextField(help_text="Specific event type within the category.")
    payload = models.JSONField(default=dict, blank=True, help_text="Event detail as JSONB.")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SystemEventManager()

    class Meta:
        db_table = "system_events"
        indexes = [
            models.Index(fields=["category", "created_at"], name="system_event_cat_idx"),
        ]

    def __str__(self) -> str:
        return f"event:{self.category}/{self.event_type}"


class AuditSignature(models.Model):
    """HMAC signature for an append-only SystemEvent payload."""

    event = models.OneToOneField(
        SystemEvent,
        on_delete=models.CASCADE,
        related_name="audit_signature",
    )
    key_id = models.CharField(max_length=64, default="default")
    payload_sha256 = models.CharField(max_length=64)
    signature = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audit_signatures"
        constraints = [
            models.CheckConstraint(
                name="audit_signature_payload_sha_hex64",
                condition=models.Q(payload_sha256__regex=r"^[0-9a-f]{64}$"),
            ),
            models.CheckConstraint(
                name="audit_signature_hex64",
                condition=models.Q(signature__regex=r"^[0-9a-f]{64}$"),
            ),
        ]

    def __str__(self) -> str:
        return f"audit-signature:{self.event_id}:{self.key_id}"
