#!/bin/sh
# =============================================================================
# ClassyFire Batch Web — Database backup script (Task 10)
#
# Runs inside the docker-compose `backup` service container where pg_dump is
# available on PATH. Environment variables injected by docker-compose:
#   POSTGRES_HOST     — DB host
#   POSTGRES_PORT     — DB port (default 5432)
#   POSTGRES_DB       — Database name
#   POSTGRES_USER     — DB username
#   PGPASSWORD        — Set from POSTGRES_PASSWORD in compose
#   BACKUP_DIR        — Where to write dumps (default /backups)
#   BACKUP_DAILY_COUNT   — Daily backups to keep (default 7)
#   BACKUP_WEEKLY_COUNT  — Weekly backups to keep (default 4)
#   BACKUP_MONTHLY_COUNT — Monthly backups to keep (default 6)
#   BACKUP_INCLUDE_SEED  — Set to "1" to include classyfire_seed table
# =============================================================================
set -eu

TIMESTAMP=$(date -u +"%Y%m%d_%H%M%S")
BACKUP_DIR="${BACKUP_DIR:-/backups}"
DUMP_FILE="${BACKUP_DIR}/classyfire_${TIMESTAMP}.dump"
MANIFEST_FILE="${BACKUP_DIR}/classyfire_${TIMESTAMP}.manifest.json"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
BACKUP_DAILY_COUNT="${BACKUP_DAILY_COUNT:-7}"
BACKUP_WEEKLY_COUNT="${BACKUP_WEEKLY_COUNT:-4}"
BACKUP_MONTHLY_COUNT="${BACKUP_MONTHLY_COUNT:-6}"
BACKUP_INCLUDE_SEED="${BACKUP_INCLUDE_SEED:-0}"

echo "[backup] Starting backup at ${TIMESTAMP}"

mkdir -p "${BACKUP_DIR}"

# Tables to dump (seed excluded by default — rebuildable from checksum + import)
TABLES="-t classyfire_live -t classification_history \
  -t batch_jobs -t batch_items \
  -t remote_tasks -t remote_queries -t remote_query_items \
  -t chemont_nodes -t seed_imports -t system_events"

if [ "${BACKUP_INCLUDE_SEED}" = "1" ]; then
  TABLES="${TABLES} -t classyfire_seed"
fi

# Dump in custom format (compressed, supports selective restore)
pg_dump \
  -h "${POSTGRES_HOST}" \
  -p "${POSTGRES_PORT}" \
  -U "${POSTGRES_USER}" \
  -d "${POSTGRES_DB}" \
  -Fc \
  ${TABLES} \
  -f "${DUMP_FILE}"

echo "[backup] Dump written: ${DUMP_FILE}"

# Compute checksum
CHECKSUM=$(sha256sum "${DUMP_FILE}" | awk '{print $1}')
echo "[backup] SHA-256: ${CHECKSUM}"

# Write manifest
cat > "${MANIFEST_FILE}" <<EOF
{
  "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "file": "classyfire_${TIMESTAMP}.dump",
  "checksum": "${CHECKSUM}",
  "status": "ok"
}
EOF

echo "[backup] Manifest written: ${MANIFEST_FILE}"

# ---------------------------------------------------------------------------
# Retention: keep DAILY_COUNT daily, WEEKLY_COUNT weekly, MONTHLY_COUNT monthly
# ---------------------------------------------------------------------------

NOW_EPOCH=$(date -u +%s)
DAY_SECS=86400
WEEK_SECS=604800
MONTH_SECS=2592000  # 30 days

DAILY_KEPT=0
WEEKLY_KEPT=0
MONTHLY_KEPT=0

# Walk manifests sorted newest-first.
# Bucket age is derived from the manifest JSON "timestamp" field (ISO-8601 UTC),
# matching the Python apply_retention() logic which also reads the manifest
# timestamp. Falling back to file mtime would diverge from Python's behaviour
# when files are copied/restored and mtime is reset.
for MFILE in $(ls -t "${BACKUP_DIR}"/*.manifest.json 2>/dev/null); do
  FILE_EPOCH=$(python3 -c "
import json, sys
from datetime import datetime, timezone
try:
    ts = json.load(open('${MFILE}')).get('timestamp','')
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    print(int(dt.timestamp()))
except Exception:
    sys.exit(1)
" 2>/dev/null || stat -c %Y "${MFILE}" 2>/dev/null || stat -f %m "${MFILE}")
  AGE_SECS=$((NOW_EPOCH - FILE_EPOCH))

  if [ "${AGE_SECS}" -lt "${WEEK_SECS}" ]; then
    BUCKET="daily"
  elif [ "${AGE_SECS}" -lt "${MONTH_SECS}" ]; then
    BUCKET="weekly"
  else
    BUCKET="monthly"
  fi

  KEEP=0
  case "${BUCKET}" in
    daily)
      if [ "${DAILY_KEPT}" -lt "${BACKUP_DAILY_COUNT}" ]; then
        DAILY_KEPT=$((DAILY_KEPT + 1))
        KEEP=1
      fi
      ;;
    weekly)
      if [ "${WEEKLY_KEPT}" -lt "${BACKUP_WEEKLY_COUNT}" ]; then
        WEEKLY_KEPT=$((WEEKLY_KEPT + 1))
        KEEP=1
      fi
      ;;
    monthly)
      if [ "${MONTHLY_KEPT}" -lt "${BACKUP_MONTHLY_COUNT}" ]; then
        MONTHLY_KEPT=$((MONTHLY_KEPT + 1))
        KEEP=1
      fi
      ;;
  esac

  if [ "${KEEP}" = "0" ]; then
    DUMP_NAME=$(python3 -c "import json,sys; print(json.load(open('${MFILE}')).get('file',''))" 2>/dev/null || true)
    if [ -n "${DUMP_NAME}" ] && [ -f "${BACKUP_DIR}/${DUMP_NAME}" ]; then
      rm "${BACKUP_DIR}/${DUMP_NAME}"
      echo "[backup] Pruned dump: ${DUMP_NAME}"
    fi
    rm "${MFILE}"
    echo "[backup] Pruned manifest: ${MFILE}"
  fi
done

echo "[backup] Retention complete (daily_kept=${DAILY_KEPT}, weekly_kept=${WEEKLY_KEPT}, monthly_kept=${MONTHLY_KEPT})"
echo "[backup] Done."
