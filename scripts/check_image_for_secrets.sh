#!/usr/bin/env bash
set -euo pipefail

image="${1:?image required}"
sentinel="${CLASSYFIRE_SENTINEL_SECRET:?CLASSYFIRE_SENTINEL_SECRET required}"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

docker history --no-trunc "$image" > "$tmpdir/history.txt"
if grep -F -- "$sentinel" "$tmpdir/history.txt" >/dev/null; then
  echo "sentinel secret found in docker history" >&2
  exit 1
fi

docker save "$image" -o "$tmpdir/image.tar"
if tar -xf "$tmpdir/image.tar" -C "$tmpdir" && grep -R -F -- "$sentinel" "$tmpdir" >/dev/null; then
  echo "sentinel secret found in docker image layers" >&2
  exit 1
fi
