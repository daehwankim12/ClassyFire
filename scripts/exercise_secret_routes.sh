#!/usr/bin/env bash
set -euo pipefail

base="${CLASSYFIRE_BASE_URL:-http://127.0.0.1}"
shared="${CLASSYFIRE_SENTINEL_SECRET:?CLASSYFIRE_SENTINEL_SECRET required}"

curl -fsS -H "Authorization: Bearer ${shared}" "${base}/health/" >/dev/null
curl -fsS -H "X-Shared-Access-Token: ${shared}" "${base}/api/stats/" >/dev/null
