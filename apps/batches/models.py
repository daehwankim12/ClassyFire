"""Batch jobs and their ordered items."""
from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q

from apps.core.enums import BatchItemStatus, BatchJobStatus, DetectedInputType

EXPORT_FMT_CHOICES = [
    ("csv", "CSV"),
    ("tsv", "TSV"),
    ("json", "JSON"),
    ("csv.gz", "CSV (gzipped)"),
    ("tsv.gz", "TSV (gzipped)"),
]

INCHIKEY_REGEX = r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$"


class BatchJob(models.Model):
    """A single uploaded batch, identified by an unguessable UUID.

    Access is gated by a per-job secret whose hash is stored in ``secret_hash``
    (the plaintext secret is only embedded in the share URL, never persisted).
    Counters are denormalised tallies updated as items are processed.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    secret_hash = models.TextField(help_text="Hash of the per-job access secret.")
    status = models.CharField(
        max_length=32,
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
    """One input row of a batch.

    ``row_order`` is unique within a job to preserve a stable input position;
    duplicate ``original_input`` values are still allowed as separate rows.
    """

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
    resolved_classification = models.ForeignKey(
        "classifications.ClassificationResult",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="batch_items",
        help_text=(
            "Immutable classification event pinned to this resolved item. The "
            "new source of truth for exports (never re-queries live)."
        ),
    )

    class Meta:
        db_table = "batch_items"
        constraints = [
            models.CheckConstraint(
                name="batch_item_inchikey_format",
                condition=Q(inchikey__isnull=True) | Q(inchikey__regex=INCHIKEY_REGEX),
            ),
            models.UniqueConstraint(
                fields=["job", "row_order"],
                name="batch_item_job_row_order_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["job", "row_order"], name="batch_item_job_order_idx"),
        ]
        ordering = ["job", "row_order"]

    def __str__(self) -> str:
        return f"item:{self.job_id}#{self.row_order} ({self.status})"


class BatchQuotaEvent(models.Model):
    """DB-backed quota accounting for job creation and export attempts."""

    KIND_JOB_CREATE = "job_create"
    KIND_EXPORT_CREATE = "export_create"

    kind = models.CharField(max_length=32)
    principal = models.CharField(max_length=128)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    row_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "batch_quota_events"
        indexes = [
            models.Index(fields=["kind", "principal", "created_at"], name="quota_kind_principal_idx"),
            models.Index(fields=["kind", "ip_address", "created_at"], name="quota_kind_ip_idx"),
            models.Index(fields=["kind", "created_at"], name="quota_kind_created_idx"),
        ]

    def __str__(self) -> str:
        return f"quota:{self.kind}:{self.principal}:{self.created_at.isoformat()}"


class ExportArtifact(models.Model):
    """DB-tracked export artifact replacing the JSON sidecar as source of truth.

    LIFECYCLE: created when an export is first generated; reused (dedup) for
    subsequent requests with the same (job, fmt, rows_signature) while the
    artifact is valid (not expired and file still present).  Pruned by the
    scheduled cleanup once ``expires_at`` is in the past.

    IMMUTABILITY: once created the content fields (path, checksum, byte_size,
    row_count, rows_signature) NEVER change.  A new export that supersedes old
    data gets a new row; the old one is kept until cleanup.

    CONSTRAINTS: checksum must be a lowercase 64-char hex SHA-256 digest;
    byte_size must be non-negative; token must be unique.
    """

    job = models.ForeignKey(
        BatchJob,
        on_delete=models.CASCADE,
        related_name="export_artifacts",
        help_text="The batch job this export was generated for.",
    )
    fmt = models.CharField(
        max_length=8,
        choices=EXPORT_FMT_CHOICES,
        help_text="Export format (csv/tsv/json/csv.gz/tsv.gz).",
    )
    token = models.CharField(
        max_length=64,
        unique=True,
        help_text="URL-safe random token used in the download URL.",
    )
    path = models.TextField(
        help_text="Absolute filesystem path of the export file."
    )
    checksum = models.CharField(
        max_length=64,
        help_text="SHA-256 hex digest of the final export file.",
    )
    byte_size = models.BigIntegerField(
        help_text="Size of the export file in bytes.",
    )
    rows_signature = models.CharField(
        max_length=64,
        help_text="SHA-256 hex of the canonical row data (used for dedup).",
    )
    row_count = models.IntegerField(
        help_text="Number of data rows (excluding header/comment lines).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        help_text="After this timestamp the artifact is expired and may be pruned.",
    )

    class Meta:
        db_table = "export_artifacts"
        constraints = [
            models.CheckConstraint(
                name="export_artifact_checksum_hex64",
                condition=Q(checksum__regex=r"^[0-9a-f]{64}$"),
            ),
            models.CheckConstraint(
                name="export_artifact_rows_sig_hex64",
                condition=Q(rows_signature__regex=r"^[0-9a-f]{64}$"),
            ),
            models.CheckConstraint(
                name="export_artifact_byte_size_nonneg",
                condition=Q(byte_size__gte=0),
            ),
        ]
        indexes = [
            models.Index(fields=["job", "fmt"], name="export_artifact_job_fmt_idx"),
            models.Index(fields=["expires_at"], name="export_artifact_expires_idx"),
            # Covers the dedup lookup: filter(job, fmt, rows_signature, expires_at)
            models.Index(
                fields=["job", "fmt", "rows_signature"],
                name="export_artifact_dedup_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"export:{self.token[:12]}…({self.fmt})"
