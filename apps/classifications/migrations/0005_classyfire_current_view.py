"""Unified current-classification read surface.

``classyfire_current`` is the single SQL view that resolves the effective
classification per InChIKey: a ``classyfire_live`` row (the dynamic, remote /
official result) takes precedence; otherwise the ``classyfire_seed_current``
bulk seed row is used. It mirrors the live-over-seed rule that previously lived
only in Python (``apps.classifications.backends``), so any-language readers can
get the same answer from ONE view instead of re-implementing the merge.

The view emits a ``tier`` discriminator ('live' | 'seed') and the union of the
provenance columns each tier carries (NULL on the side that does not apply), so
the existing per-tier row converters keep producing byte-for-byte identical
``ClassificationLookupResult`` objects.

``CREATE OR REPLACE VIEW`` keeps the dependency on ``classyfire_seed_current``
intact across ``seed_activate`` view swaps (those also use REPLACE, preserving
the column set).
"""
from django.db import migrations

_CREATE_VIEW = """
CREATE OR REPLACE VIEW classyfire_current AS
SELECT
    cl.inchikey,
    'live'::text                  AS tier,
    cl.source_type,
    cl.source_priority,
    cl.source_ref,
    cl.classification_version,
    cl.retrieved_at,
    cl.remote_query_id,
    cl.result_id,
    cl.labels,
    NULL::bigint                  AS seed_source_id,
    NULL::text                    AS seed_version,
    kn.name        AS kingdom_name,        kn.chemont_id  AS kingdom_chemont_id,
    scn.name       AS superclass_name,     scn.chemont_id AS superclass_chemont_id,
    cn.name        AS class_name,          cn.chemont_id  AS class_chemont_id,
    sbn.name       AS subclass_name,       sbn.chemont_id AS subclass_chemont_id,
    dpn.name       AS direct_parent_name,  dpn.chemont_id AS direct_parent_chemont_id
FROM classyfire_live cl
LEFT JOIN chemont_nodes kn  ON kn.numeric_id  = cl.kingdom_id
LEFT JOIN chemont_nodes scn ON scn.numeric_id = cl.superclass_id
LEFT JOIN chemont_nodes cn  ON cn.numeric_id  = cl.class_id
LEFT JOIN chemont_nodes sbn ON sbn.numeric_id = cl.subclass_id
LEFT JOIN chemont_nodes dpn ON dpn.numeric_id = cl.direct_parent_id
UNION ALL
SELECT
    cs.inchikey,
    'seed'::text                  AS tier,
    NULL::varchar                 AS source_type,
    NULL::integer                 AS source_priority,
    NULL::text                    AS source_ref,
    NULL::text                    AS classification_version,
    NULL::timestamptz             AS retrieved_at,
    NULL::text                    AS remote_query_id,
    NULL::bigint                  AS result_id,
    NULL::jsonb                   AS labels,
    cs.seed_source_id,
    cs.seed_version,
    kn.name, kn.chemont_id,
    scn.name, scn.chemont_id,
    cn.name, cn.chemont_id,
    sbn.name, sbn.chemont_id,
    dpn.name, dpn.chemont_id
FROM classyfire_seed_current cs
LEFT JOIN chemont_nodes kn  ON kn.numeric_id  = cs.kingdom_id
LEFT JOIN chemont_nodes scn ON scn.numeric_id = cs.superclass_id
LEFT JOIN chemont_nodes cn  ON cn.numeric_id  = cs.class_id
LEFT JOIN chemont_nodes sbn ON sbn.numeric_id = cs.subclass_id
LEFT JOIN chemont_nodes dpn ON dpn.numeric_id = cs.direct_parent_id
WHERE NOT EXISTS (
    SELECT 1 FROM classyfire_live cl WHERE cl.inchikey = cs.inchikey
);
"""

_DROP_VIEW = "DROP VIEW IF EXISTS classyfire_current;"


class Migration(migrations.Migration):

    dependencies = [
        ("classifications", "0004_source_artifact_bytes_key_checks"),
        # The view JOINs ``chemont_nodes``; ensure the canonical table name exists
        # (chemont 0002 renames chemont_node -> chemont_nodes) before this runs.
        ("chemont", "0002_rename_chemont_node_table"),
    ]

    operations = [
        migrations.RunSQL(sql=_CREATE_VIEW, reverse_sql=_DROP_VIEW),
    ]
