"""The live read surface: ``classyfire_current`` view (live-only) + live indexes.

``classyfire_current`` resolves the effective classification per InChIKey from
``classyfire_live`` only (the seedless caching server has no seed tier), so the
Python row converters and any other-language readers get a stable shape from ONE
view.

The two ``classyfire_live`` indexes are expression/ordered indexes (not
model-Meta), so they are declared here as raw SQL:
  - ``live_priority_retrieved_idx``  — live-precedence ordering
  - ``live_inchikey14_idx``          — 14-char skeleton-prefix lookups

The view JOINs ``chemont_nodes`` and reads ``classyfire_live``, so it depends on
both the classifications and chemont initial migrations.
"""
from django.db import migrations

_CREATE_VIEW = """
CREATE VIEW classyfire_current AS
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
LEFT JOIN chemont_nodes dpn ON dpn.numeric_id = cl.direct_parent_id;
"""
_DROP_VIEW = "DROP VIEW IF EXISTS classyfire_current;"

_CREATE_LIVE_PRIORITY_IDX = """
CREATE INDEX IF NOT EXISTS live_priority_retrieved_idx
    ON classyfire_live (source_priority DESC, retrieved_at DESC);
"""
_DROP_LIVE_PRIORITY_IDX = "DROP INDEX IF EXISTS live_priority_retrieved_idx;"

_CREATE_LIVE_IK14_IDX = """
CREATE INDEX IF NOT EXISTS live_inchikey14_idx
    ON classyfire_live ((left(inchikey, 14)));
"""
_DROP_LIVE_IK14_IDX = "DROP INDEX IF EXISTS live_inchikey14_idx;"


class Migration(migrations.Migration):

    dependencies = [
        ("classifications", "0001_initial"),
        ("chemont", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=_CREATE_VIEW, reverse_sql=_DROP_VIEW),
        migrations.RunSQL(
            sql=_CREATE_LIVE_PRIORITY_IDX, reverse_sql=_DROP_LIVE_PRIORITY_IDX
        ),
        migrations.RunSQL(sql=_CREATE_LIVE_IK14_IDX, reverse_sql=_DROP_LIVE_IK14_IDX),
    ]
