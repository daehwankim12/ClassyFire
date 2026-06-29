"""Admin registration for classification tables (all read-only).

These tables are append-only / immutable by contract — the live pointer, the
immutable result events, archived history, and the seed snapshot. Read-only
admin enforces that no operator edit can break those invariants.
"""
from __future__ import annotations

from django.contrib import admin

from apps.classifications.models import (
    ClassificationHistory,
    ClassificationResult,
    ClassyfireLive,
    ClassyfireSeed,
    SourceArtifact,
)
from apps.core.admin_base import ReadOnlyModelAdmin


@admin.register(ClassificationResult)
class ClassificationResultAdmin(ReadOnlyModelAdmin):
    list_display = (
        "id",
        "requested_inchikey",
        "returned_inchikey",
        "kind",
        "source_type",
        "source_priority",
        "retrieved_at",
        "created_at",
    )
    list_filter = ("kind", "source_type")
    search_fields = ("requested_inchikey", "returned_inchikey", "remote_query_id")


@admin.register(ClassyfireLive)
class ClassyfireLiveAdmin(ReadOnlyModelAdmin):
    list_display = (
        "inchikey",
        "source_type",
        "source_priority",
        "retrieved_at",
    )
    list_filter = ("source_type",)
    search_fields = ("inchikey",)


@admin.register(ClassyfireSeed)
class ClassyfireSeedAdmin(ReadOnlyModelAdmin):
    list_display = ("inchikey", "inchikey14", "seed_version", "seed_source_id")
    search_fields = ("inchikey", "inchikey14")


@admin.register(ClassificationHistory)
class ClassificationHistoryAdmin(ReadOnlyModelAdmin):
    list_display = (
        "inchikey",
        "source_type",
        "source_priority",
        "retrieved_at",
        "archived_at",
    )
    list_filter = ("source_type",)
    search_fields = ("inchikey",)


@admin.register(SourceArtifact)
class SourceArtifactAdmin(ReadOnlyModelAdmin):
    list_display = (
        "id",
        "source_type",
        "name",
        "checksum",
        "byte_size",
        "imported_at",
    )
    list_filter = ("source_type",)
    search_fields = ("name", "checksum")
