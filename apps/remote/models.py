"""Remote ClassyFire work queue: tasks, queries, and their mapping.

``RemoteTask`` is the language-agnostic queue. Workers in any language claim
work with ``FOR UPDATE SKIP LOCKED`` over this table using the
(state, priority, next_run_at, lock_owner, locked_at, attempt) columns; see
docs/state-machines.md. The partial-unique index on ``inchikey`` over the
active states is the global dedupe of outstanding work.
"""
from __future__ import annotations

from django.db import models
from django.db.models import Q

from apps.core.enums import ACTIVE_TASK_STATES, QueryState, TaskState


class RemoteTask(models.Model):
    """A unit of outstanding remote classification work for one InChIKey."""

    inchikey = models.CharField(max_length=27, help_text="Target InChIKey.")
    compound = models.ForeignKey(
        "compounds.CompoundStructure",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="remote_tasks",
    )

    # --- queue protocol columns (language-agnostic) ---
    state = models.CharField(
        max_length=16, choices=TaskState.choices, default=TaskState.READY
    )
    priority = models.IntegerField(
        default=0, help_text="Higher is claimed first."
    )
    attempt = models.IntegerField(default=0, help_text="Retry attempt counter.")
    next_run_at = models.DateTimeField(
        null=True, blank=True, help_text="Earliest time the task may be claimed."
    )
    lock_owner = models.TextField(
        null=True, blank=True, help_text="Worker id currently holding the task."
    )
    locked_at = models.DateTimeField(
        null=True, blank=True, help_text="When the current lock was taken."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "remote_task"
        constraints = [
            models.CheckConstraint(
                name="remote_task_inchikey_len27",
                condition=Q(inchikey__length=27),
            ),
            # Only ONE active task per InChIKey: a partial unique index over the
            # active states (everything except DONE). DONE rows do not conflict.
            models.UniqueConstraint(
                fields=["inchikey"],
                name="remote_task_active_inchikey_unique",
                condition=Q(state__in=ACTIVE_TASK_STATES),
            ),
        ]
        indexes = [
            # Supports the FOR UPDATE SKIP LOCKED claim query.
            models.Index(
                fields=["state", "priority", "next_run_at"],
                name="remote_task_claim_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"task:{self.inchikey} ({self.state})"


class RemoteQuery(models.Model):
    """A single submitted ClassyFire query/batch and its polling lifecycle."""

    remote_query_id = models.TextField(
        null=True, blank=True, help_text="Upstream query id once submitted."
    )
    payload_hash = models.TextField(help_text="Hash of the submitted payload.")
    state = models.CharField(
        max_length=16, choices=QueryState.choices, default=QueryState.SUBMITTED
    )
    next_poll_at = models.DateTimeField(null=True, blank=True)
    raw_response = models.JSONField(
        null=True, blank=True, help_text="Latest raw upstream response as JSONB."
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "remote_query"
        indexes = [
            models.Index(fields=["state", "next_poll_at"], name="remote_query_poll_idx"),
        ]

    def __str__(self) -> str:
        return f"query:{self.remote_query_id or self.pk} ({self.state})"


class RemoteQueryItem(models.Model):
    """Maps a RemoteQuery to the RemoteTask/BatchItem/compound it resolves."""

    query = models.ForeignKey(
        RemoteQuery, on_delete=models.CASCADE, related_name="items"
    )
    task = models.ForeignKey(
        RemoteTask,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="query_items",
    )
    batch_item = models.ForeignKey(
        "batches.BatchItem",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="query_items",
    )
    compound = models.ForeignKey(
        "compounds.CompoundStructure",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="query_items",
    )
    inchikey = models.CharField(
        max_length=27, null=True, blank=True, help_text="InChIKey this mapping targets."
    )

    class Meta:
        db_table = "remote_query_item"
        constraints = [
            models.CheckConstraint(
                name="remote_query_item_inchikey_len27",
                condition=Q(inchikey__isnull=True) | Q(inchikey__length=27),
            ),
        ]
        indexes = [
            models.Index(fields=["query"], name="remote_query_item_query_idx"),
        ]

    def __str__(self) -> str:
        return f"qitem:{self.query_id}->{self.inchikey}"
