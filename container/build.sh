#!/usr/bin/env bash
# Stage the sysperf package into the build context and build the image.
#
# Only the runtime payload is staged: the package's modules, PowerShell helpers
# and the static UI. The user-maintained catalogs (components.json,
# hw_watchlist.json) are deliberately left out -- they are mutable state, so the
# image symlinks them to /data instead.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SYSPERF_SRC:-$HERE/../src/sysperf}"
STAGE="$HERE/app/sysperf"

[ -f "$SRC/__init__.py" ] || { echo "no sysperf package at $SRC" >&2; exit 1; }

echo "==> Staging application from $SRC"
rm -rf "$HERE/app"
mkdir -p "$STAGE/static"
cp "$SRC"/*.py "$SRC"/*.ps1 "$STAGE/"
cp "$SRC/static/index.html" "$STAGE/static/index.html"

echo "==> Resolving python:3.12-slim digest"
docker pull --quiet python:3.12-slim >/dev/null
DIGEST="$(docker image inspect python:3.12-slim \
          --format '{{index .RepoDigests 0}}' | cut -d@ -f2)"
if [[ ! "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "could not resolve a digest for python:3.12-slim" >&2
    exit 1
fi
echo "    $DIGEST"
sed -e "s|python@sha256:PYTHON_DIGEST_PLACEHOLDER|python@$DIGEST|" \
    -e "s|^FROM python@sha256:[0-9a-f]\{64\}$|FROM python@$DIGEST|" \
    "$HERE/Dockerfile" > "$HERE/Dockerfile.resolved"

echo "==> Building image"
docker build -f "$HERE/Dockerfile.resolved" \
    --build-arg "SYSPERF_UID=$(id -u)" \
    -t sysperf-dashboard:local "$HERE"

echo "==> Done. Image: sysperf-dashboard:local"
