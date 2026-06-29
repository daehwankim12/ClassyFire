"""Admin registration for ChemOnt ontology nodes (read-only)."""
from __future__ import annotations

from django.contrib import admin

from apps.chemont.models import ChemOntNode
from apps.core.admin_base import ReadOnlyModelAdmin


@admin.register(ChemOntNode)
class ChemOntNodeAdmin(ReadOnlyModelAdmin):
    list_display = ("numeric_id", "chemont_id", "name", "parent", "ontology_version")
    search_fields = ("chemont_id", "name")
    list_select_related = ("parent",)
