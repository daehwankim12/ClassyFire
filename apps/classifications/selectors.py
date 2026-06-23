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
