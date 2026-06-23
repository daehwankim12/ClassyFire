-- Classification lookup: live-first then seed-current.
-- Always query exact 27-char InChIKey only (P-02: no auto-apply for 14-char prefix).
-- Live result takes precedence; within equal priority, newest retrieved_at wins.

-- Step 1: Try classyfire_live (highest priority, newest wins)
SELECT
    cl.inchikey,
    cl.source_type,
    cl.source_priority,
    cl.source_ref,
    cl.classification_version,
    cl.retrieved_at,
    cl.remote_query_id,
    -- ChemOnt slots (names via JOIN)
    kn.name  AS kingdom_name,  kn.chemont_id  AS kingdom_id,
    scn.name AS superclass_name, scn.chemont_id AS superclass_id,
    cn.name  AS class_name,    cn.chemont_id  AS class_id,
    sbn.name AS subclass_name, sbn.chemont_id AS subclass_id,
    dpn.name AS direct_parent_name, dpn.chemont_id AS direct_parent_id,
    'LIVE' AS tier
FROM classyfire_live cl
LEFT JOIN chemont_nodes kn  ON kn.numeric_id = cl.kingdom_id
LEFT JOIN chemont_nodes scn ON scn.numeric_id = cl.superclass_id
LEFT JOIN chemont_nodes cn  ON cn.numeric_id  = cl.class_id
LEFT JOIN chemont_nodes sbn ON sbn.numeric_id = cl.subclass_id
LEFT JOIN chemont_nodes dpn ON dpn.numeric_id = cl.direct_parent_id
WHERE cl.inchikey = %(inchikey)s   -- exact 27-char match only
ORDER BY cl.source_priority DESC, cl.retrieved_at DESC
LIMIT 1

UNION ALL

-- Step 2: Fall back to classyfire_seed_current
SELECT
    cs.inchikey,
    'SEED'                AS source_type,
    0                     AS source_priority,
    NULL                  AS source_ref,
    cs.seed_version       AS classification_version,
    NULL                  AS retrieved_at,
    NULL                  AS remote_query_id,
    kn.name  AS kingdom_name,  kn.chemont_id  AS kingdom_id,
    scn.name AS superclass_name, scn.chemont_id AS superclass_id,
    cn.name  AS class_name,    cn.chemont_id  AS class_id,
    sbn.name AS subclass_name, sbn.chemont_id AS subclass_id,
    dpn.name AS direct_parent_name, dpn.chemont_id AS direct_parent_id,
    'SEED' AS tier
FROM classyfire_seed_current cs
LEFT JOIN chemont_nodes kn  ON kn.numeric_id = cs.kingdom_id
LEFT JOIN chemont_nodes scn ON scn.numeric_id = cs.superclass_id
LEFT JOIN chemont_nodes cn  ON cn.numeric_id  = cs.class_id
LEFT JOIN chemont_nodes sbn ON sbn.numeric_id = cs.subclass_id
LEFT JOIN chemont_nodes dpn ON dpn.numeric_id = cs.direct_parent_id
WHERE cs.inchikey = %(inchikey)s   -- exact 27-char match only
LIMIT 1;
