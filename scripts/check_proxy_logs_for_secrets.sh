#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   check_proxy_logs_for_secrets.sh [--secret-file FILE] LOG_FILE [SENTINEL...]
#
# Fails (exit 1) if any secret appears in LOG_FILE. Secrets come from, in order:
#   --secret-file FILE : one secret per non-empty line (the RUNTIME-generated
#                        capability secrets emitted by exercise_secret_routes.sh).
#   SENTINEL...        : explicit literals passed on the command line.
#   CLASSYFIRE_SENTINEL_SECRET : the shared sentinel (required only when neither a
#                        secret file nor explicit sentinels are supplied).

secret_file=""
if [ "${1:-}" = "--secret-file" ]; then
  secret_file="${2:?--secret-file requires a path}"
  shift 2
fi

log_file="${1:?log file path required}"
shift || true

fail_if_leaked() {
  secret="$1"
  [ -z "$secret" ] && return 0
  if grep -F -- "$secret" "$log_file" >/dev/null; then
    echo "secret leaked to logs" >&2
    exit 1
  fi
}

if [ -n "$secret_file" ]; then
  while IFS= read -r line; do
    fail_if_leaked "$line"
  done < "$secret_file"
fi

if [ "$#" -gt 0 ]; then
  sentinels="$*"
elif [ -n "$secret_file" ]; then
  # A secret file already supplied the runtime values; the shared sentinel is
  # an optional extra here (do NOT hard-require it).
  sentinels="${CLASSYFIRE_SENTINEL_SECRET:-}"
else
  sentinels="${CLASSYFIRE_SENTINEL_SECRET:?sentinel required}"
fi

for sentinel in ${sentinels}; do
  fail_if_leaked "$sentinel"
done
