#!/bin/sh
# =============================================================================
# ClassyFire Batch Web — Database backup script
#
# Runs inside the docker-compose `backup` service container where pg_dump is
# available on PATH. Environment variables injected by docker-compose:
#   POSTGRES_HOST     — DB host
#   POSTGRES_PORT     — DB port (default 5432)
#   POSTGRES_DB       — Database name
#   POSTGRES_USER     — DB username
#   PGPASSWORD        — Set from POSTGRES_PASSWORD in compose
#   BACKUP_DIR        — Where to write dumps (default /backups)
#   MEDIA_DIR         — Path to the media (upload) volume (default /app/media)
#   BACKUP_DAILY_COUNT   — Daily backups to keep (default 7)
#   BACKUP_WEEKLY_COUNT  — Weekly backups to keep (default 4)
#   BACKUP_MONTHLY_COUNT — Monthly backups to keep (default 6)
#
# Exclusion policy
# ────────────────
# The seed tables (classyfire_seed, classyfire_seed_v*, classyfire_seed_current
# view) are RE-IMPORTABLE from Zenodo source artifacts, so they are excluded
# to keep backups small.  ALL other tables — compounds, batch jobs/items,
# remote tasks + provenance, classification live/history, operations — are
# included in the dump.
#
# Alpine compatibility
# ────────────────────
# The backup service image is postgres:16-alpine (BusyBox). This script uses
# ONLY tools guaranteed present on that image:
#   - find (BusyBox find with -mtime support)
#   - awk / sh arithmetic
#   - sha256sum (coreutils, present in postgres:16-alpine)
# It does NOT depend on python3.
# =============================================================================
set -euo pipefail

TIMESTAMP=$(date -u +"%Y%m%d_%H%M%S")
BACKUP_DIR="${BACKUP_DIR:-/backups}"
MEDIA_DIR="${MEDIA_DIR:-/app/media}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
BACKUP_DAILY_COUNT="${BACKUP_DAILY_COUNT:-7}"
BACKUP_WEEKLY_COUNT="${BACKUP_WEEKLY_COUNT:-4}"
BACKUP_MONTHLY_COUNT="${BACKUP_MONTHLY_COUNT:-6}"

DUMP_FILE="${BACKUP_DIR}/classyfire_${TIMESTAMP}.dump"
MEDIA_ARCHIVE="${BACKUP_DIR}/classyfire_media_${TIMESTAMP}.tar.gz"
MANIFEST_FILE="${BACKUP_DIR}/classyfire_${TIMESTAMP}.manifest.json"

echo "[backup] Starting backup at ${TIMESTAMP}"

mkdir -p "${BACKUP_DIR}"

# ---------------------------------------------------------------------------
# 1. Database dump — full DB EXCLUDING only the rebuildable seed tables.
#    classyfire_seed*  covers: classyfire_seed (base table),
#                              classyfire_seed_v<N> (versioned tables),
#                              classyfire_seed_current (view — pg_dump skips
#                              views when using --exclude-table anyway).
# ---------------------------------------------------------------------------
pg_dump \
  -h "${POSTGRES_HOST}" \
  -p "${POSTGRES_PORT}" \
  -U "${POSTGRES_USER}" \
  -d "${POSTGRES_DB}" \
  -Fc \
  --exclude-table="classyfire_seed*" \
  -f "${DUMP_FILE}"

echo "[backup] DB dump written: ${DUMP_FILE}"

# Compute DB dump checksum
DUMP_CHECKSUM=$(sha256sum "${DUMP_FILE}" | awk '{print $1}')
echo "[backup] DB dump SHA-256: ${DUMP_CHECKSUM}"

# ---------------------------------------------------------------------------
# 2. Media (upload + export) artifact archive.
#    Only run if the directory exists and is non-empty.
# ---------------------------------------------------------------------------
MEDIA_CHECKSUM=""
if [ -d "${MEDIA_DIR}" ] && [ "$(find "${MEDIA_DIR}" -mindepth 1 -maxdepth 1 | head -c1)" != "" ]; then
  tar -czf "${MEDIA_ARCHIVE}" -C "$(dirname "${MEDIA_DIR}")" "$(basename "${MEDIA_DIR}")"
  MEDIA_CHECKSUM=$(sha256sum "${MEDIA_ARCHIVE}" | awk '{print $1}')
  echo "[backup] Media archive written: ${MEDIA_ARCHIVE} (SHA-256: ${MEDIA_CHECKSUM})"
else
  echo "[backup] Media dir empty or absent — skipping media archive"
fi

# ---------------------------------------------------------------------------
# 3. Write manifest (JSON, no python3 required — use heredoc with sh vars)
# ---------------------------------------------------------------------------
BACKUP_TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Build optional media fields
if [ -n "${MEDIA_CHECKSUM}" ]; then
  MEDIA_JSON="  \"media_file\": \"classyfire_media_${TIMESTAMP}.tar.gz\",
  \"media_checksum\": \"${MEDIA_CHECKSUM}\","
else
  MEDIA_JSON="  \"media_file\": null,
  \"media_checksum\": null,"
fi

cat > "${MANIFEST_FILE}" <<EOF
{
  "timestamp": "${BACKUP_TS}",
  "file": "classyfire_${TIMESTAMP}.dump",
  "checksum": "${DUMP_CHECKSUM}",
${MEDIA_JSON}
  "excludes": "classyfire_seed*",
  "status": "ok"
}
EOF

echo "[backup] Manifest written: ${MANIFEST_FILE}"

# ---------------------------------------------------------------------------
# 4. Retention pruning — POSIX find, no python3.
#
# Strategy: read the manifest "timestamp" field with awk/sed, compare mtime.
# Since the manifest is always written at backup time, file mtime (seconds
# since epoch via find -newer or find -mtime) is a reliable age proxy on
# alpine/BusyBox.  We sort manifests newest-first and bucket them.
#
#   daily   : < 7 days old   — keep BACKUP_DAILY_COUNT
#   weekly  : 7–27 days old  — keep BACKUP_WEEKLY_COUNT
#   monthly : 28+ days old   — keep BACKUP_MONTHLY_COUNT
#
# BusyBox find supports -mtime N (integer days).  We use -mtime +N to find
# files older than N days, and prune the excess.  Sorting by name (which
# embeds the timestamp) gives newest-first order.
# ---------------------------------------------------------------------------

# Count how many manifests exist in each bucket, then prune oldest excess.
# We walk manifests sorted newest-first (reverse lexicographic on filename).

DAILY_KEPT=0
WEEKLY_KEPT=0
MONTHLY_KEPT=0

# List manifests newest-first via reverse sort.
MANIFESTS=$(ls -r "${BACKUP_DIR}"/*.manifest.json 2>/dev/null || true)

NOW_EPOCH=$(date -u +%s)

for MFILE in ${MANIFESTS}; do
  # Derive file age from its own mtime (stat -c on GNU, stat -f on BSD/busybox).
  # BusyBox stat supports: stat -c %Y <file>
  FILE_EPOCH=$(stat -c %Y "${MFILE}" 2>/dev/null || stat -f %m "${MFILE}" 2>/dev/null || echo "0")
  AGE_SECS=$((NOW_EPOCH - FILE_EPOCH))

  # Bucket by age
  if [ "${AGE_SECS}" -lt 604800 ]; then      # < 7 days
    BUCKET="daily"
  elif [ "${AGE_SECS}" -lt 2419200 ]; then   # < 28 days
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
    # Extract dump filename from manifest using awk (no python3 needed).
    DUMP_NAME=$(awk -F'"' '/"file"/{print $4; exit}' "${MFILE}" 2>/dev/null || true)
    if [ -n "${DUMP_NAME}" ] && [ -f "${BACKUP_DIR}/${DUMP_NAME}" ]; then
      rm "${BACKUP_DIR}/${DUMP_NAME}"
      echo "[backup] Pruned dump: ${DUMP_NAME}"
    fi
    # Also prune associated media archive (same timestamp stem).
    STEM=$(basename "${MFILE}" .manifest.json)
    # The media archive uses a different naming: classyfire_media_<TIMESTAMP>.tar.gz
    # Extract timestamp from stem: classyfire_<TIMESTAMP>
    TS_PART=$(echo "${STEM}" | sed 's/^classyfire_//')
    MEDIA_NAME="classyfire_media_${TS_PART}.tar.gz"
    if [ -f "${BACKUP_DIR}/${MEDIA_NAME}" ]; then
      rm "${BACKUP_DIR}/${MEDIA_NAME}"
      echo "[backup] Pruned media archive: ${MEDIA_NAME}"
    fi
    rm "${MFILE}"
    echo "[backup] Pruned manifest: $(basename "${MFILE}")"
  fi
done

echo "[backup] Retention complete (daily_kept=${DAILY_KEPT}, weekly_kept=${WEEKLY_KEPT}, monthly_kept=${MONTHLY_KEPT})"
echo "[backup] Done."
