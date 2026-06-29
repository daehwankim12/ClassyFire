"""Admin registration for compound structures and usage (read-only)."""
from __future__ import annotations

from django.contrib import admin

from apps.compounds.models import CompoundStructure, CompoundUsage
from apps.core.admin_base import ReadOnlyModelAdmin


@admin.register(CompoundStructure)
class CompoundStructureAdmin(ReadOnlyModelAdmin):
    list_display = ("inchikey", "canonical_smiles", "created_at")
    search_fields = ("inchikey",)


@admin.register(CompoundUsage)
class CompoundUsageAdmin(ReadOnlyModelAdmin):
    list_display = (
        "inchikey",
        "seen_count",
        "waiting_job_count",
        "first_seen",
        "last_seen",
    )
    search_fields = ("inchikey",)
