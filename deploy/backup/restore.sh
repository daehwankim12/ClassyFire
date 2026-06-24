#!/bin/sh
# =============================================================================
# ClassyFire Batch Web — Database restore script
#
# Usage:
#   POSTGRES_HOST=<host> POSTGRES_PORT=<port> POSTGRES_USER=<user> \
#   POSTGRES_DB=<target_db> PGPASSWORD=<password> \
#   BACKUP_DIR=/backups MANIFEST_FILE=classyfire_<TS>.manifest.json \
#     sh /restore.sh
#
# Environment variables:
#   POSTGRES_HOST     — DB host (default: db)
#   POSTGRES_PORT     — DB port (default: 5432)
#   POSTGRES_DB       — Target database name to restore INTO
#   POSTGRES_USER     — DB username
#   PGPASSWORD        — DB password (plain; used by pg_restore)
#   BACKUP_DIR        — Directory containing backup files (default: /backups)
#   MANIFEST_FILE     — Specific manifest filename to restore from.
#                       If not set, the most recent manifest in BACKUP_DIR
#                       is used.
#   RESTORE_MEDIA_DIR — If set, restore the media archive to this directory.
#                       If not set, media restore is skipped.
#   RESTORE_DROP_DB   — Set to "1" to DROP + CREATE the target DB before
#                       restore.  Default "0" (restore with --clean
#                       --if-exists without dropping).
#
# Target-state expectations:
#   - In RESTORE_DROP_DB=0 mode (default): the target DB must already exist.
#     pg_restore --clean --if-exists drops and recreates each object in
#     the dump before restoring.  Suitable for restoring over an existing DB.
#   - In RESTORE_DROP_DB=1 mode: a superuser-level psql connection is used
#     to DROP + CREATE the database.  Use when restoring into a blank cluster.
#
# Checksum verification:
#   The script verifies the SHA-256 checksum of the dump file against the
#   value in the manifest before any restore operation begins.  The restore
#   is aborted if the checksum does not match.
#
# After restore:
#   The seed tables (classyfire_seed*) are NOT in the dump — they were
#   excluded at backup time because they are re-importable from the Zenodo
#   source artifact.  Re-seed after restore with:
#     python manage.py seed_import --profile FULL   (or LITE for testing)
#   Then activate with:
#     python manage.py seed_activate
# =============================================================================
set -euo pipefail

POSTGRES_HOST="${POSTGRES_HOST:-db}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-classyfire}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
RESTORE_DROP_DB="${RESTORE_DROP_DB:-0}"

# ── Validate required variables ────────────────────────────────────────────
if [ -z "${POSTGRES_DB:-}" ]; then
  echo "[restore] ERROR: POSTGRES_DB must be set" >&2
  exit 1
fi
if [ -z "${PGPASSWORD:-}" ]; then
  echo "[restore] ERROR: PGPASSWORD must be set" >&2
  exit 1
fi

# ── Locate the manifest ────────────────────────────────────────────────────
if [ -n "${MANIFEST_FILE:-}" ]; then
  MANIFEST="${BACKUP_DIR}/${MANIFEST_FILE}"
else
  # Most recent manifest in BACKUP_DIR (lexicographic sort on TS prefix).
  MANIFEST=$(ls -r "${BACKUP_DIR}"/*.manifest.json 2>/dev/null | head -1 || true)
fi

if [ -z "${MANIFEST}" ] || [ ! -f "${MANIFEST}" ]; then
  echo "[restore] ERROR: No manifest found in ${BACKUP_DIR}" >&2
  exit 1
fi

echo "[restore] Using manifest: ${MANIFEST}"

# ── Parse manifest fields with awk (no python3 / jq required) ─────────────
DUMP_FILE=$(awk -F'"' '/"file"/{print $4; exit}' "${MANIFEST}")
EXPECTED_CHECKSUM=$(awk -F'"' '/"checksum"/{print $4; exit}' "${MANIFEST}")
MEDIA_FILE=$(awk -F'"' '/"media_file"/{print $4; exit}' "${MANIFEST}" || true)

DUMP_PATH="${BACKUP_DIR}/${DUMP_FILE}"

if [ ! -f "${DUMP_PATH}" ]; then
  echo "[restore] ERROR: Dump file not found: ${DUMP_PATH}" >&2
  exit 1
fi

# ── Checksum verification ──────────────────────────────────────────────────
echo "[restore] Verifying checksum for ${DUMP_FILE} ..."
ACTUAL_CHECKSUM=$(sha256sum "${DUMP_PATH}" | awk '{print $1}')

if [ "${ACTUAL_CHECKSUM}" != "${EXPECTED_CHECKSUM}" ]; then
  echo "[restore] ERROR: Checksum mismatch!" >&2
  echo "[restore]   Expected: ${EXPECTED_CHECKSUM}" >&2
  echo "[restore]   Actual:   ${ACTUAL_CHECKSUM}" >&2
  exit 1
fi
echo "[restore] Checksum OK: ${ACTUAL_CHECKSUM}"

# ── Optional: drop + recreate database ────────────────────────────────────
if [ "${RESTORE_DROP_DB}" = "1" ]; then
  echo "[restore] Dropping and recreating database: ${POSTGRES_DB}"
  psql \
    -h "${POSTGRES_HOST}" \
    -p "${POSTGRES_PORT}" \
    -U "${POSTGRES_USER}" \
    -d postgres \
    -c "DROP DATABASE IF EXISTS \"${POSTGRES_DB}\";"
  psql \
    -h "${POSTGRES_HOST}" \
    -p "${POSTGRES_PORT}" \
    -U "${POSTGRES_USER}" \
    -d postgres \
    -c "CREATE DATABASE \"${POSTGRES_DB}\" OWNER \"${POSTGRES_USER}\";"
fi

# ── Restore DB dump ────────────────────────────────────────────────────────
echo "[restore] Restoring ${DUMP_FILE} into database: ${POSTGRES_DB}"
pg_restore \
  -h "${POSTGRES_HOST}" \
  -p "${POSTGRES_PORT}" \
  -U "${POSTGRES_USER}" \
  -d "${POSTGRES_DB}" \
  --clean \
  --if-exists \
  "${DUMP_PATH}"

echo "[restore] Database restore complete."

# ── Restore media archive (optional) ──────────────────────────────────────
if [ -n "${RESTORE_MEDIA_DIR:-}" ] && [ -n "${MEDIA_FILE:-}" ]; then
  MEDIA_PATH="${BACKUP_DIR}/${MEDIA_FILE}"
  if [ -f "${MEDIA_PATH}" ]; then
    echo "[restore] Restoring media archive: ${MEDIA_FILE} -> ${RESTORE_MEDIA_DIR}"
    mkdir -p "${RESTORE_MEDIA_DIR}"
    # Restore into the *parent* of RESTORE_MEDIA_DIR so the archive's
    # top-level directory ('media') is unpacked correctly.
    tar -xzf "${MEDIA_PATH}" -C "$(dirname "${RESTORE_MEDIA_DIR}")"
    echo "[restore] Media restore complete."
  else
    echo "[restore] WARNING: media archive not found: ${MEDIA_PATH}" >&2
  fi
else
  echo "[restore] Media restore skipped (RESTORE_MEDIA_DIR not set or no media archive in manifest)."
fi

echo "[restore] Done."
echo "[restore]"
echo "[restore] NOTE: The seed tables (classyfire_seed*) were excluded from"
echo "[restore]       this backup.  Re-seed with:"
echo "[restore]         python manage.py seed_import --profile FULL"
echo "[restore]         python manage.py seed_activate"
