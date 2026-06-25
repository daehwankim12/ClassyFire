#!/usr/bin/env bash
# Backup→restore smoke test skeleton.
# Task 21 extends this with the full 14-step fixture (DB, media, export,
# source-artifact creation and per-table restore assertions).
set -euo pipefail

DB_NAME="${POSTGRES_DB:-classyfire}"
DB_USER="${POSTGRES_USER:-classyfire}"
DB_HOST="${POSTGRES_HOST:-127.0.0.1}"
DB_PORT="${POSTGRES_PORT:-5432}"

RESTORE_DB="${RESTORE_DB:-classyfire_smoke_restore}"
DUMP_FILE="$(mktemp /tmp/cf_smoke_XXXXXX.dump)"
trap 'rm -f "$DUMP_FILE"' EXIT

echo "==> smoke: dumping $DB_NAME"
pg_dump \
  --host="$DB_HOST" \
  --port="$DB_PORT" \
  --username="$DB_USER" \
  --format=custom \
  --no-password \
  "$DB_NAME" \
  --file="$DUMP_FILE"

echo "==> smoke: creating restore target $RESTORE_DB"
PGPASSWORD="${POSTGRES_PASSWORD:-classyfire}" psql \
  --host="$DB_HOST" \
  --port="$DB_PORT" \
  --username="$DB_USER" \
  --command="DROP DATABASE IF EXISTS $RESTORE_DB;" \
  --command="CREATE DATABASE $RESTORE_DB OWNER $DB_USER;"

echo "==> smoke: restoring into $RESTORE_DB"
pg_restore \
  --host="$DB_HOST" \
  --port="$DB_PORT" \
  --username="$DB_USER" \
  --dbname="$RESTORE_DB" \
  --no-password \
  --exit-on-error \
  "$DUMP_FILE"

echo "==> smoke: asserting restored DB is non-empty"
ROW_COUNT=$(PGPASSWORD="${POSTGRES_PASSWORD:-classyfire}" psql \
  --host="$DB_HOST" \
  --port="$DB_PORT" \
  --username="$DB_USER" \
  --dbname="$RESTORE_DB" \
  --tuples-only \
  --command="SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';")

if [ "${ROW_COUNT// /}" -lt 1 ]; then
  echo "restore_smoke_test: restored DB has no public tables" >&2
  exit 1
fi

echo "==> smoke: PASSED (${ROW_COUNT// /} public tables restored)"

echo "==> smoke: cleaning up restore target"
PGPASSWORD="${POSTGRES_PASSWORD:-classyfire}" psql \
  --host="$DB_HOST" \
  --port="$DB_PORT" \
  --username="$DB_USER" \
  --command="DROP DATABASE IF EXISTS $RESTORE_DB;"
