"""Classification result tables: immutable seed, current live, and history.

These tables hold the actual ClassyFire classifications keyed by InChIKey, with
explicit ChemOnt slot FKs (kingdom/superclass/class/subclass/direct_parent),
raw JSON provenance, and an integer source priority for overwrite arbitration.
"""
from __future__ import annotations

from django.db import models
from django.db.models import Q

from apps.core.enums import SourceType


def _chemont_slot(slot_name: str) -> models.ForeignKey:
    """A nullable FK to a ChemOnt node for one classification slot."""
    return models.ForeignKey(
        "chemont.ChemOntNode",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        db_column=f"{slot_name}_id",
    )


class ClassyfireSeed(models.Model):
    """Immutable seed shape (requirements section 5.2).

    One row per InChIKey. This is the base physical table; Task 4 builds the
    versioned-table swap and the ``classyfire_seed_current`` view on top of it.
    The CHAR(27)/CHAR(14) check constraints are defined here.
    """

    inchikey = models.CharField(
        max_length=27,
        primary_key=True,
        help_text="Full 27-char InChIKey (exact length enforced by DB).",
    )
    inchikey14 = models.CharField(
        max_length=14,
        help_text="14-char skeleton (first block) of the InChIKey.",
    )
    pubchem_cid = models.BigIntegerField(null=True, blank=True)
    zinc_id = models.TextField(null=True, blank=True)
    representative_smiles = models.TextField(null=True, blank=True)

    kingdom = _chemont_slot("kingdom")
    superclass = _chemont_slot("superclass")
    class_node = _chemont_slot("class")
    subclass = _chemont_slot("subclass")
    direct_parent = _chemont_slot("direct_parent")

    other_labels = models.JSONField(
        null=True,
        blank=True,
        help_text="Additional ChemOnt labels / alternative parents as JSONB.",
    )
    seed_source_id = models.BigIntegerField(
        help_text="FK-by-value to the operations.SeedImport that produced this row."
    )
    seed_version = models.TextField(help_text="Seed dataset version string.")

    class Meta:
        db_table = "classyfire_seed"
        constraints = [
            models.CheckConstraint(
                name="classyfire_seed_inchikey_len27",
                condition=Q(inchikey__length=27),
            ),
            models.CheckConstraint(
                name="classyfire_seed_inchikey14_len14",
                condition=Q(inchikey14__length=14),
            ),
        ]
        indexes = [
            models.Index(fields=["inchikey14"], name="seed_inchikey14_idx"),
        ]

    def __str__(self) -> str:
        return f"seed:{self.inchikey}@{self.seed_version}"


class ClassyfireLive(models.Model):
    """Current best live/archive/official classification for an InChIKey.

    There is at most one *current* row per InChIKey; prior rows are archived to
    :class:`ClassificationHistory`. ``source_priority`` mirrors
    :data:`apps.core.enums.SOURCE_PRIORITY` as a stored INTEGER so overwrite
    arbitration is language-agnostic.
    """

    inchikey = models.CharField(max_length=27)
    source_type = models.CharField(max_length=32, choices=SourceType.choices)
    source_priority = models.IntegerField(
        help_text="Stored copy of SOURCE_PRIORITY[source_type] for arbitration."
    )

    kingdom = _chemont_slot("kingdom")
    superclass = _chemont_slot("superclass")
    class_node = _chemont_slot("class")
    subclass = _chemont_slot("subclass")
    direct_parent = _chemont_slot("direct_parent")

    labels = models.JSONField(
        null=True,
        blank=True,
        help_text="Class label columns / alternative parents as JSONB.",
    )
    raw_json = models.JSONField(
        null=True, blank=True, help_text="Raw upstream response, kept as provenance."
    )
    remote_query_id = models.TextField(null=True, blank=True)
    source_ref = models.TextField(
        null=True, blank=True, help_text="Reference to the originating import/query."
    )
    classification_version = models.TextField(null=True, blank=True)
    retrieved_at = models.DateTimeField(help_text="When this result was retrieved.")

    class Meta:
        db_table = "classyfire_live"
        constraints = [
            models.CheckConstraint(
                name="classyfire_live_inchikey_len27",
                condition=Q(inchikey__length=27),
            ),
            models.UniqueConstraint(
                fields=["inchikey"], name="classyfire_live_unique_current"
            ),
        ]
        indexes = [
            models.Index(fields=["inchikey"], name="live_inchikey_idx"),
        ]

    def __str__(self) -> str:
        return f"live:{self.inchikey} ({self.source_type})"


class ClassificationHistory(models.Model):
    """Archived snapshot of a superseded :class:`ClassyfireLive` row."""

    inchikey = models.CharField(max_length=27)
    source_type = models.CharField(max_length=32, choices=SourceType.choices)
    source_priority = models.IntegerField()
    payload = models.JSONField(
        help_text="Full snapshot of the superseded live row as JSONB."
    )
    retrieved_at = models.DateTimeField(
        help_text="When the snapshotted result was originally retrieved."
    )
    archived_at = models.DateTimeField(
        auto_now_add=True, help_text="When this snapshot was archived."
    )

    class Meta:
        db_table = "classification_history"
        verbose_name_plural = "classification history"
        constraints = [
            models.CheckConstraint(
                name="classification_history_inchikey_len27",
                condition=Q(inchikey__length=27),
            ),
        ]
        indexes = [
            models.Index(fields=["inchikey"], name="history_inchikey_idx"),
        ]

    def __str__(self) -> str:
        return f"history:{self.inchikey}@{self.archived_at:%Y-%m-%d}"
