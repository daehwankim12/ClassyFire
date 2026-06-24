from __future__ import annotations

from typing import Any

from django.db import connection

_LIVE_QUERY = """
    SELECT
        cl.inchikey,
        cl.source_type,
        cl.source_priority,
        cl.source_ref,
        cl.classification_version,
        cl.retrieved_at,
        cl.remote_query_id,
        cl.result_id,
        cl.labels,
        kn.name        AS kingdom_name,
        kn.chemont_id  AS kingdom_chemont_id,
        scn.name       AS superclass_name,
        scn.chemont_id AS superclass_chemont_id,
        cn.name        AS class_name,
        cn.chemont_id  AS class_chemont_id,
        sbn.name       AS subclass_name,
        sbn.chemont_id AS subclass_chemont_id,
        dpn.name       AS direct_parent_name,
        dpn.chemont_id AS direct_parent_chemont_id
    FROM classyfire_live cl
    LEFT JOIN chemont_nodes kn  ON kn.numeric_id = cl.kingdom_id
    LEFT JOIN chemont_nodes scn ON scn.numeric_id = cl.superclass_id
    LEFT JOIN chemont_nodes cn  ON cn.numeric_id  = cl.class_id
    LEFT JOIN chemont_nodes sbn ON sbn.numeric_id = cl.subclass_id
    LEFT JOIN chemont_nodes dpn ON dpn.numeric_id = cl.direct_parent_id
    WHERE cl.inchikey = %s
    ORDER BY cl.source_priority DESC, cl.retrieved_at DESC
    LIMIT 1
"""

_SEED_QUERY = """
    SELECT
        cs.inchikey,
        cs.seed_version,
        cs.seed_source_id,
        kn.name        AS kingdom_name,
        kn.chemont_id  AS kingdom_chemont_id,
        scn.name       AS superclass_name,
        scn.chemont_id AS superclass_chemont_id,
        cn.name        AS class_name,
        cn.chemont_id  AS class_chemont_id,
        sbn.name       AS subclass_name,
        sbn.chemont_id AS subclass_chemont_id,
        dpn.name       AS direct_parent_name,
        dpn.chemont_id AS direct_parent_chemont_id
    FROM classyfire_seed_current cs
    LEFT JOIN chemont_nodes kn  ON kn.numeric_id = cs.kingdom_id
    LEFT JOIN chemont_nodes scn ON scn.numeric_id = cs.superclass_id
    LEFT JOIN chemont_nodes cn  ON cn.numeric_id  = cs.class_id
    LEFT JOIN chemont_nodes sbn ON sbn.numeric_id = cs.subclass_id
    LEFT JOIN chemont_nodes dpn ON dpn.numeric_id = cs.direct_parent_id
    WHERE cs.inchikey = %s
    LIMIT 1
"""


def _row_to_dict(cursor: Any, row: tuple[Any, ...]) -> dict[str, Any]:
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row, strict=True))


def get_live_classification(inchikey: str) -> dict[str, Any] | None:
    with connection.cursor() as cur:
        cur.execute(_LIVE_QUERY, [inchikey])
        row: tuple[Any, ...] | None = cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(cur, row)


def get_seed_classification(inchikey: str) -> dict[str, Any] | None:
    with connection.cursor() as cur:
        cur.execute(_SEED_QUERY, [inchikey])
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(cur, row)


# --- Bulk variants (WHERE inchikey IN (...)) for chunked ingest (F-11) -------

_LIVE_BULK_QUERY = _LIVE_QUERY.replace(
    "WHERE cl.inchikey = %s\n    ORDER BY cl.source_priority DESC, cl.retrieved_at DESC\n    LIMIT 1",
    "WHERE cl.inchikey = ANY(%s)\n    ORDER BY cl.inchikey, cl.source_priority DESC, cl.retrieved_at DESC",
)

_SEED_BULK_QUERY = _SEED_QUERY.replace(
    "WHERE cs.inchikey = %s\n    LIMIT 1",
    "WHERE cs.inchikey = ANY(%s)",
)


def get_live_classifications(inchikeys: list[str]) -> dict[str, dict[str, Any]]:
    """Bulk live lookup. Returns ``{inchikey: row}`` keeping ONLY the
    highest-priority/newest live row per inchikey (mirrors the single-row
    ``ORDER BY source_priority DESC, retrieved_at DESC LIMIT 1`` semantics).
    """
    if not inchikeys:
        return {}
    out: dict[str, dict[str, Any]] = {}
    with connection.cursor() as cur:
        cur.execute(_LIVE_BULK_QUERY, [list(inchikeys)])
        cols = [d[0] for d in cur.description]
        for row in cur.fetchall():
            data = dict(zip(cols, row, strict=True))
            # First row per inchikey wins (query is ordered so the winner is
            # first within each inchikey group).
            out.setdefault(data["inchikey"], data)
    return out


def get_seed_classifications(inchikeys: list[str]) -> dict[str, dict[str, Any]]:
    """Bulk seed lookup. Returns ``{inchikey: row}`` for current-seed hits."""
    if not inchikeys:
        return {}
    out: dict[str, dict[str, Any]] = {}
    with connection.cursor() as cur:
        cur.execute(_SEED_BULK_QUERY, [list(inchikeys)])
        cols = [d[0] for d in cur.description]
        for row in cur.fetchall():
            data = dict(zip(cols, row, strict=True))
            out.setdefault(data["inchikey"], data)
    return out
