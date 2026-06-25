#!/usr/bin/env bash
set -euo pipefail

log_file="${1:?log file path required}"
shift

if [ "$#" -eq 0 ]; then
  sentinels="${CLASSYFIRE_SENTINEL_SECRET:?sentinel required}"
else
  sentinels="$*"
fi

for sentinel in ${sentinels}; do
  if grep -F -- "$sentinel" "$log_file" >/dev/null; then
    echo "secret leaked to logs: $sentinel" >&2
    exit 1
  fi
done
