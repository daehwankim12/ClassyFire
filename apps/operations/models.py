"""Operational tables: the system event log and audit signatures."""
from __future__ import annotations

from typing import Any

from django.db import models

from apps.core.enums import EventCategory


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
