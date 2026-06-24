#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${1:?image name required}"
SENTINEL="${CLASSYFIRE_SENTINEL_SECRET:?CLASSYFIRE_SENTINEL_SECRET required}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

docker save "${IMAGE_NAME}" -o "${TMP_DIR}/image.tar"
if grep -R "${SENTINEL}" "${TMP_DIR}" >/dev/null 2>&1; then
  echo "sentinel secret found in image layers" >&2
  exit 1
fi
docker history --no-trunc "${IMAGE_NAME}" | grep -F "${SENTINEL}" && exit 1
exit 0
