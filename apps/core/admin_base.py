"""Shared admin building blocks for the read-only operations console.

The whole admin surface is a *console*: operators inspect durable state and
trigger a small, curated set of audited operational actions. Direct row
editing is forbidden everywhere because many tables are append-only / immutable
by contract (classification events, the live pointer, system events, remote
attempt logs, audit signatures); letting the admin mutate them would break
those invariants. :class:`ReadOnlyModelAdmin` enforces that uniformly. The
curated mutating actions are NOT left open to any viewer: they declare
``permissions=("operate",)`` so Django gates them on
:meth:`ReadOnlyModelAdmin.has_operate_permission` (superuser-only), and the
custom pause/resume views enforce the same check — a view-only operator can
inspect but cannot trigger state changes.
"""
from __future__ import annotations

import uuid
from typing import Any

from django.contrib import admin
from django.db.models import Model, QuerySet
from django.http import HttpRequest


class ReadOnlyModelAdmin(admin.ModelAdmin):
    """A :class:`~django.contrib.admin.ModelAdmin` that forbids all mutation.

    Add / change / delete are denied; every concrete field is rendered
    read-only so the detail view is immutable too. ``show_full_result_count``
    is disabled so the changelist never runs an unbounded ``COUNT(*)`` over a
    large table. Subclasses keep view, search, filter, and registered actions.
    """

    show_full_result_count = False

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: Model | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: Model | None = None
    ) -> bool:
        return False

    def get_readonly_fields(
        self, request: HttpRequest, obj: Model | None = None
    ) -> list[str]:
        return [field.name for field in self.model._meta.fields]

    def has_operate_permission(self, request: HttpRequest) -> bool:
        """Permission gate for the curated mutating admin actions.

        Row editing stays forbidden via ``has_change_permission=False``; the
        operational actions instead declare ``permissions=("operate",)`` so
        Django filters them out unless this returns True. Restricted to
        superusers — triggering state changes (pausing remote, retrying tasks,
        resetting provider auth, recomputing counters, failing submissions) is
        an admin-only capability, never available to a view-only operator.
        """
        return bool(request.user.is_active and request.user.is_superuser)


class UuidSearchMixin:
    """Adds full-UUID search on a configured field without breaking text search.

    Searching a UUID column with the default ``icontains`` lookup is fragile, so
    instead: ``uuid_search_field`` (e.g. ``"id"`` or ``"job_id"``) is matched
    only when the search term parses as a UUID; non-UUID terms fall through to
    the normal text ``search_fields`` unchanged. Requires a non-empty
    ``search_fields`` for the search box to appear.
    """

    uuid_search_field: str | None = None

    def get_search_results(
        self,
        request: HttpRequest,
        queryset: QuerySet[Any],
        search_term: str,
    ) -> tuple[QuerySet[Any], bool]:
        qs, may_have_duplicates = super().get_search_results(  # type: ignore[misc]
            request, queryset, search_term
        )
        field = self.uuid_search_field
        if field and search_term:
            try:
                parsed = uuid.UUID(search_term.strip())
            except (ValueError, AttributeError):
                return qs, may_have_duplicates
            # OR the UUID match into the ALREADY-SCOPED ``queryset`` (the
            # changelist base with active list_filters / get_queryset applied),
            # NOT the unfiltered default manager — otherwise the UUID search
            # would surface rows excluded by the current filters or scoping.
            qs |= queryset.filter(**{field: parsed})
        return qs, may_have_duplicates
