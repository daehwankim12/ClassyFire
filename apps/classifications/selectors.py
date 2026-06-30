from __future__ import annotations

from typing import Any

from django.db import connection


def _row_to_dict(cursor: Any, row: tuple[Any, ...]) -> dict[str, Any]:
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row, strict=True))


# --- Unified surface: classyfire_current view (live-only) ---------------------
# The view emits one row per inchikey from the live cache, so a single query
# resolves the effective classification into a ``ClassificationLookupResult``.

_CURRENT_QUERY = "SELECT * FROM classyfire_current WHERE inchikey = %s LIMIT 1"
_CURRENT_BULK_QUERY = "SELECT * FROM classyfire_current WHERE inchikey = ANY(%s)"


def get_current_classification(inchikey: str) -> dict[str, Any] | None:
    """Resolved current classification (live-only) for one InChIKey."""
    with connection.cursor() as cur:
        cur.execute(_CURRENT_QUERY, [inchikey])
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(cur, row)


def get_current_classifications(inchikeys: list[str]) -> dict[str, dict[str, Any]]:
    """Bulk resolved lookup. Returns ``{inchikey: row}``; the view guarantees
    at most one row per inchikey (live-only)."""
    if not inchikeys:
        return {}
    out: dict[str, dict[str, Any]] = {}
    with connection.cursor() as cur:
        cur.execute(_CURRENT_BULK_QUERY, [list(inchikeys)])
        cols = [d[0] for d in cur.description]
        for row in cur.fetchall():
            data = dict(zip(cols, row, strict=True))
            out.setdefault(data["inchikey"], data)
    return out
