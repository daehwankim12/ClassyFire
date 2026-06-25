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
#   RESTORE_MEDIA_DIR          — If set, restore the media archive to this
#                                directory. If not set, media restore is skipped.
#   RESTORE_EXPORT_DIR         — If set, restore the exports archive to this
#                                directory. If not set, exports restore is skipped.
#   RESTORE_SOURCE_ARTIFACT_DIR — If set, restore the source_artifacts archive
#                                to this directory. If not set, source artifacts
#                                restore is skipped.
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
set -eu

POSTGRES_HOST="${POSTGRES_HOST:-db}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-classyfire}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
RESTORE_DROP_DB="${RESTORE_DROP_DB:-0}"
RESTORE_UPLOAD_DIR="${RESTORE_UPLOAD_DIR:-}"

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
# A "media_file" of null/empty in the manifest means no media archive was
# produced at backup time — skip cleanly.
if [ -n "${RESTORE_MEDIA_DIR:-}" ] && [ -n "${MEDIA_FILE:-}" ] && [ "${MEDIA_FILE}" != "null" ]; then
  MEDIA_PATH="${BACKUP_DIR}/${MEDIA_FILE}"
  if [ -f "${MEDIA_PATH}" ]; then
    # Verify the media archive checksum BEFORE extracting, mirroring the
    # DB-dump checksum logic above. Fail loudly on mismatch.
    EXPECTED_MEDIA_CHECKSUM=$(awk -F'"' '/"media_checksum"/{print $4; exit}' "${MANIFEST}" || true)
    if [ -n "${EXPECTED_MEDIA_CHECKSUM}" ] && [ "${EXPECTED_MEDIA_CHECKSUM}" != "null" ]; then
      echo "[restore] Verifying media checksum for ${MEDIA_FILE} ..."
      ACTUAL_MEDIA_CHECKSUM=$(sha256sum "${MEDIA_PATH}" | awk '{print $1}')
      if [ "${ACTUAL_MEDIA_CHECKSUM}" != "${EXPECTED_MEDIA_CHECKSUM}" ]; then
        echo "[restore] ERROR: Media checksum mismatch!" >&2
        echo "[restore]   Expected: ${EXPECTED_MEDIA_CHECKSUM}" >&2
        echo "[restore]   Actual:   ${ACTUAL_MEDIA_CHECKSUM}" >&2
        exit 1
      fi
      echo "[restore] Media checksum OK: ${ACTUAL_MEDIA_CHECKSUM}"
    else
      echo "[restore] WARNING: no media_checksum in manifest — extracting without verification" >&2
    fi
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

# ── Restore exports archive (optional) ────────────────────────────────────
# Reads artifact_archives.exports from the manifest (if present).
EXPORTS_FILE=$(awk -F'"' '/"exports":/{for(i=1;i<=NF;i++) if($i=="file"){print $(i+2); exit}}' "${MANIFEST}" || true)
if [ -n "${RESTORE_EXPORT_DIR:-}" ] && [ -n "${EXPORTS_FILE:-}" ] && [ "${EXPORTS_FILE}" != "null" ]; then
  EXPORTS_PATH="${BACKUP_DIR}/${EXPORTS_FILE}"
  if [ -f "${EXPORTS_PATH}" ]; then
    EXPECTED_EXPORTS_CHECKSUM=$(awk -F'"' '/"exports":/{for(i=1;i<=NF;i++) if($i=="checksum"){print $(i+2); exit}}' "${MANIFEST}" || true)
    if [ -n "${EXPECTED_EXPORTS_CHECKSUM}" ] && [ "${EXPECTED_EXPORTS_CHECKSUM}" != "null" ]; then
      echo "[restore] Verifying exports checksum for ${EXPORTS_FILE} ..."
      ACTUAL_EXPORTS_CHECKSUM=$(sha256sum "${EXPORTS_PATH}" | awk '{print $1}')
      if [ "${ACTUAL_EXPORTS_CHECKSUM}" != "${EXPECTED_EXPORTS_CHECKSUM}" ]; then
        echo "[restore] ERROR: Exports checksum mismatch!" >&2
        echo "[restore]   Expected: ${EXPECTED_EXPORTS_CHECKSUM}" >&2
        echo "[restore]   Actual:   ${ACTUAL_EXPORTS_CHECKSUM}" >&2
        exit 1
      fi
      echo "[restore] Exports checksum OK: ${ACTUAL_EXPORTS_CHECKSUM}"
    else
      echo "[restore] WARNING: no exports checksum in manifest — extracting without verification" >&2
    fi
    echo "[restore] Restoring exports archive: ${EXPORTS_FILE} -> ${RESTORE_EXPORT_DIR}"
    mkdir -p "${RESTORE_EXPORT_DIR}"
    tar -xzf "${EXPORTS_PATH}" -C "$(dirname "${RESTORE_EXPORT_DIR}")"
    echo "[restore] Exports restore complete."
  else
    echo "[restore] WARNING: exports archive not found: ${EXPORTS_PATH}" >&2
  fi
else
  echo "[restore] Exports restore skipped (RESTORE_EXPORT_DIR not set or no exports archive in manifest)."
fi

# ── Restore source_artifacts archive (optional) ───────────────────────────
# Reads artifact_archives.source_artifacts from the manifest (if present).
SOURCE_ARTIFACTS_FILE=$(awk -F'"' '/"source_artifacts":/{for(i=1;i<=NF;i++) if($i=="file"){print $(i+2); exit}}' "${MANIFEST}" || true)
if [ -n "${RESTORE_SOURCE_ARTIFACT_DIR:-}" ] && [ -n "${SOURCE_ARTIFACTS_FILE:-}" ] && [ "${SOURCE_ARTIFACTS_FILE}" != "null" ]; then
  SOURCE_ARTIFACTS_PATH="${BACKUP_DIR}/${SOURCE_ARTIFACTS_FILE}"
  if [ -f "${SOURCE_ARTIFACTS_PATH}" ]; then
    EXPECTED_SOURCE_CHECKSUM=$(awk -F'"' '/"source_artifacts":/{for(i=1;i<=NF;i++) if($i=="checksum"){print $(i+2); exit}}' "${MANIFEST}" || true)
    if [ -n "${EXPECTED_SOURCE_CHECKSUM}" ] && [ "${EXPECTED_SOURCE_CHECKSUM}" != "null" ]; then
      echo "[restore] Verifying source_artifacts checksum for ${SOURCE_ARTIFACTS_FILE} ..."
      ACTUAL_SOURCE_CHECKSUM=$(sha256sum "${SOURCE_ARTIFACTS_PATH}" | awk '{print $1}')
      if [ "${ACTUAL_SOURCE_CHECKSUM}" != "${EXPECTED_SOURCE_CHECKSUM}" ]; then
        echo "[restore] ERROR: Source artifacts checksum mismatch!" >&2
        echo "[restore]   Expected: ${EXPECTED_SOURCE_CHECKSUM}" >&2
        echo "[restore]   Actual:   ${ACTUAL_SOURCE_CHECKSUM}" >&2
        exit 1
      fi
      echo "[restore] Source artifacts checksum OK: ${ACTUAL_SOURCE_CHECKSUM}"
    else
      echo "[restore] WARNING: no source_artifacts checksum in manifest — extracting without verification" >&2
    fi
    echo "[restore] Restoring source_artifacts archive: ${SOURCE_ARTIFACTS_FILE} -> ${RESTORE_SOURCE_ARTIFACT_DIR}"
    mkdir -p "${RESTORE_SOURCE_ARTIFACT_DIR}"
    tar -xzf "${SOURCE_ARTIFACTS_PATH}" -C "$(dirname "${RESTORE_SOURCE_ARTIFACT_DIR}")"
    echo "[restore] Source artifacts restore complete."
  else
    echo "[restore] WARNING: source artifacts archive not found: ${SOURCE_ARTIFACTS_PATH}" >&2
  fi
else
  echo "[restore] Source artifacts restore skipped (RESTORE_SOURCE_ARTIFACT_DIR not set or no archive in manifest)."
fi

echo "[restore] Done."
echo "[restore]"
echo "[restore] NOTE: The seed tables (classyfire_seed*) were excluded from"
echo "[restore]       this backup.  Re-seed with:"
echo "[restore]         python manage.py seed_import --profile FULL"
echo "[restore]         python manage.py seed_activate"
