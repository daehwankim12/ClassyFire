"""Batch jobs and their ordered items."""
from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q

from apps.core.enums import BatchItemStatus, BatchJobStatus, DetectedInputType


class BatchJob(models.Model):
    """A single uploaded batch, identified by an unguessable UUID.

    Access is gated by a per-job secret whose hash is stored in ``secret_hash``
    (the plaintext secret is only embedded in the share URL, never persisted).
    Counters are denormalised tallies updated as items are processed.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    secret_hash = models.TextField(help_text="Hash of the per-job access secret.")
    status = models.CharField(
        max_length=16,
        choices=BatchJobStatus.choices,
        default=BatchJobStatus.PENDING,
        help_text="Coarse job lifecycle status.",
    )

    total_count = models.IntegerField(default=0)
    completed_count = models.IntegerField(default=0)
    local_hit_count = models.IntegerField(default=0)
    seed_hit_count = models.IntegerField(default=0)
    remote_pending_count = models.IntegerField(default=0)
    need_structure_count = models.IntegerField(default=0)
    invalid_count = models.IntegerField(default=0)

    remote_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "batch_jobs"

    def __str__(self) -> str:
        return f"job:{self.id} ({self.status})"


class BatchItem(models.Model):
    """One input row of a batch. Row order is preserved; duplicates are allowed."""

    job = models.ForeignKey(
        BatchJob, on_delete=models.CASCADE, related_name="items"
    )
    row_order = models.IntegerField(
        help_text="0-based input position; preserves order, duplicates allowed."
    )
    external_id = models.TextField(
        null=True, blank=True, help_text="Caller-supplied id for this row, if any."
    )
    original_input = models.TextField(help_text="Raw input string for this row.")
    detected_type = models.CharField(
        max_length=16,
        choices=DetectedInputType.choices,
        default=DetectedInputType.UNKNOWN,
    )
    compound = models.ForeignKey(
        "compounds.CompoundStructure",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="batch_items",
        help_text="Linked parsed structure once normalized.",
    )
    inchikey = models.CharField(
        max_length=27,
        null=True,
        blank=True,
        help_text="Resolved InChIKey once normalized.",
    )
    status = models.CharField(
        max_length=32,
        choices=BatchItemStatus.choices,
        default=BatchItemStatus.PENDING_NORMALIZATION,
    )
    message = models.TextField(
        null=True, blank=True, help_text="Human-readable detail for the status."
    )
    classification_snapshot = models.JSONField(
        null=True,
        blank=True,
        help_text="Pinned classification labels/provenance used by this item.",
    )

    class Meta:
        db_table = "batch_items"
        constraints = [
            models.CheckConstraint(
                name="batch_item_inchikey_len27",
                condition=Q(inchikey__isnull=True) | Q(inchikey__length=27),
            ),
        ]
        indexes = [
            models.Index(fields=["job", "row_order"], name="batch_item_job_order_idx"),
        ]
        ordering = ["job", "row_order"]

    def __str__(self) -> str:
        return f"item:{self.job_id}#{self.row_order} ({self.status})"
