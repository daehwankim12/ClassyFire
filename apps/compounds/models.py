"""Compound structures and usage tracking (keyed by InChIKey)."""
from __future__ import annotations

from django.db import models
from django.db.models import Q


class CompoundStructure(models.Model):
    """A parsed chemical structure, keyed conceptually by its InChIKey.

    Stores the original input plus the RDKit-derived canonical forms and a
    JSONB bag of parse metadata (warnings, sanitization flags, etc.).
    """

    original_input = models.TextField(help_text="Raw input string as supplied.")
    original_smiles = models.TextField(null=True, blank=True)
    canonical_smiles = models.TextField(null=True, blank=True)
    inchi = models.TextField(null=True, blank=True)
    inchikey = models.CharField(max_length=27, help_text="Full 27-char InChIKey.")
    parse_metadata = models.JSONField(
        default=dict, blank=True, help_text="RDKit parse details / warnings as JSONB."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "compound_structure"
        constraints = [
            models.CheckConstraint(
                name="compound_structure_inchikey_len27",
                condition=Q(inchikey__length=27),
            ),
        ]
        indexes = [
            models.Index(fields=["inchikey"], name="compound_inchikey_idx"),
        ]

    def __str__(self) -> str:
        return f"structure:{self.inchikey}"


class CompoundUsage(models.Model):
    """One row per InChIKey tracking how often / recently it has been seen."""

    inchikey = models.CharField(
        max_length=27, primary_key=True, help_text="Full 27-char InChIKey."
    )
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    seen_count = models.IntegerField(default=0)
    waiting_job_count = models.IntegerField(default=0)

    class Meta:
        db_table = "compound_usage"
        constraints = [
            models.CheckConstraint(
                name="compound_usage_inchikey_len27",
                condition=Q(inchikey__length=27),
            ),
        ]

    def __str__(self) -> str:
        return f"usage:{self.inchikey} (x{self.seen_count})"
