from django.db import migrations

_CREATE_VIEW = """
CREATE OR REPLACE VIEW classyfire_seed_current AS SELECT * FROM classyfire_seed;
"""

_DROP_VIEW = """
DROP VIEW IF EXISTS classyfire_seed_current;
"""

_CREATE_LIVE_PRIORITY_IDX = """
CREATE INDEX IF NOT EXISTS live_priority_retrieved_idx
    ON classyfire_live (source_priority DESC, retrieved_at DESC);
"""

_DROP_LIVE_PRIORITY_IDX = """
DROP INDEX IF EXISTS live_priority_retrieved_idx;
"""

_CREATE_LIVE_IK14_IDX = """
CREATE INDEX IF NOT EXISTS live_inchikey14_idx
    ON classyfire_live ((left(inchikey, 14)));
"""

_DROP_LIVE_IK14_IDX = """
DROP INDEX IF EXISTS live_inchikey14_idx;
"""


class Migration(migrations.Migration):
    dependencies = [("classifications", "0001_initial")]

    operations = [
        migrations.RunSQL(sql=_CREATE_VIEW, reverse_sql=_DROP_VIEW),
        migrations.RunSQL(sql=_CREATE_LIVE_PRIORITY_IDX, reverse_sql=_DROP_LIVE_PRIORITY_IDX),
        migrations.RunSQL(sql=_CREATE_LIVE_IK14_IDX, reverse_sql=_DROP_LIVE_IK14_IDX),
    ]
