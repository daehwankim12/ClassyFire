#!/usr/bin/env bash
set -euo pipefail

LOG_FILE="${1:?log file required}"
SENTINEL="${CLASSYFIRE_SENTINEL_SECRET:?CLASSYFIRE_SENTINEL_SECRET required}"

if grep -F "${SENTINEL}" "${LOG_FILE}" >/dev/null 2>&1; then
  echo "sentinel secret found in proxy/app logs" >&2
  exit 1
fi
exit 0
