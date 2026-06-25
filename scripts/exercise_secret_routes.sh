#!/usr/bin/env bash
set -euo pipefail

base="${CLASSYFIRE_BASE_URL:-http://127.0.0.1}"
shared="${CLASSYFIRE_SENTINEL_SECRET:?CLASSYFIRE_SENTINEL_SECRET required}"
job_secret="sentinel-job-secret"
upload_name="sentinel-upload-filename.csv"
upload_file="$(mktemp)"
trap 'rm -f "$upload_file" /tmp/export.json' EXIT
printf 'id,smiles\nrow1,CCO\n' > "$upload_file"

curl -fsS -H "Authorization: Bearer ${shared}" "${base}/health/" >/dev/null
curl -fsS -H "X-Shared-Access-Token: ${shared}" "${base}/api/stats/" >/dev/null

legacy_status="$(curl -sS -o /dev/null -w '%{http_code}' "${base}/api/jobs/00000000-0000-0000-0000-000000000000?secret=${job_secret}")"
test "$legacy_status" != "200"

submit_response="$(curl -fsS -H "Authorization: Bearer ${shared}" -F "file=@${upload_file};filename=${upload_name};type=text/csv" "${base}/api/jobs")"
job_id="$(printf '%s' "$submit_response" | python -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')"
result_secret="$(printf '%s' "$submit_response" | python -c 'import json,sys; print(json.load(sys.stdin)["result_secret"])')"

curl -fsS -H "X-Job-Secret: ${result_secret}" "${base}/api/jobs/${job_id}" >/dev/null
curl -fsS -L "${base}/jobs/${job_id}/claim/${result_secret}/" >/dev/null
curl -fsS -H "X-Job-Secret: ${result_secret}" -X POST -H "Content-Type: application/json" -d '{"format":"csv"}' "${base}/api/jobs/${job_id}/exports" >/tmp/export.json
token="$(python -c 'import json; print(json.load(open("/tmp/export.json"))["token"])')"
curl -fsS "${base}/api/exports/${token}" >/dev/null || true
