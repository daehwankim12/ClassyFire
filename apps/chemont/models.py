"""ChemOnt ontology nodes (the ClassyFire taxonomy tree)."""
from __future__ import annotations

from django.db import models


class ChemOntNode(models.Model):
    """A single node in the ChemOnt ontology.

    Keyed by ``numeric_id`` (the integer extracted from a CHEMONT id) so that
    classification slot FKs are compact INTEGER columns. The textual
    ``chemont_id`` (e.g. ``"CHEMONT:0000000"``) is a stable unique alternate key.
    """

    numeric_id = models.IntegerField(
        primary_key=True,
        help_text="Numeric ChemOnt id, e.g. 0 for CHEMONT:0000000.",
    )
    chemont_id = models.TextField(
        unique=True,
        help_text='Canonical ChemOnt identifier, e.g. "CHEMONT:0000000".',
    )
    name = models.TextField(help_text="Human-readable node name.")
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
        help_text="Parent node; null for the ontology root.",
    )
    ontology_version = models.TextField(
        help_text="ChemOnt ontology version this node was imported from."
    )

    class Meta:
        db_table = "chemont_nodes"
        verbose_name = "ChemOnt node"
        verbose_name_plural = "ChemOnt nodes"

    def __str__(self) -> str:
        return f"{self.chemont_id} ({self.name})"
