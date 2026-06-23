#!/bin/sh
# =============================================================================
# ClassyFire Batch Web — Database backup script (placeholder)
#
# Full implementation in Task 10.  This placeholder keeps docker-compose.yml
# valid by providing the referenced file.
#
# Expected environment variables (injected by docker-compose):
#   POSTGRES_HOST
#   POSTGRES_DB
#   POSTGRES_USER
#   PGPASSWORD   (set from POSTGRES_PASSWORD in compose)
# =============================================================================
set -eu

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_DIR="${BACKUP_DIR:-/backups}"
DUMP_FILE="${BACKUP_DIR}/classyfire_${TIMESTAMP}.sql.gz"

echo "[backup] Starting backup at ${TIMESTAMP}"

# Task 10 will implement:
#   1. pg_dump | gzip > "$DUMP_FILE"
#   2. Retention cleanup (keep N days)
#   3. Optional offsite upload

echo "[backup] Placeholder — full implementation in Task 10"
