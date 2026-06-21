#!/usr/bin/env bash
# Build the embedded portable Python runtime that backs the steerable-sidecar.
#
# Output: ./resources/python-runtime/<platform>/  — picked up by electron-builder
# via the `extraResources` entry in package.json.
#
# Pass --skip-wheels to skip rebuilding the framework wheels (useful when you
# just want to re-run the runtime build with the wheels already in place).
#
# Pass --target {host|all|<name>} to control which platform(s) to build for.
# Default is the host platform — use `all` only when packaging an installer
# matrix from one developer machine.
set -euo pipefail

cd "$(dirname "$0")/.."
FW="${STEERABLE_FRAMEWORK_DIR:-../../..}"
TARGET="host"
SKIP_WHEELS=0
EXTRA_BUILD_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)         TARGET="$2"; shift 2 ;;
    --skip-wheels)    SKIP_WHEELS=1; shift ;;
    --strip-stdlib|--aggressive)
                      EXTRA_BUILD_ARGS+=("$1"); shift ;;
    --)               shift ;;
    *)                EXTRA_BUILD_ARGS+=("$1"); shift ;;
  esac
done

if [[ ! -d "$FW/packages/sidecar/build" ]]; then
  echo "ERROR: $FW does not look like a steerable-framework checkout." >&2
  exit 1
fi

if [[ "$SKIP_WHEELS" -eq 0 ]]; then
  bash ./scripts/prepare-framework-wheels.sh
else
  echo "[prepare-sidecar] --skip-wheels set, assuming $FW/dist/py/ is current"
fi

WHEELS_ABS="$(cd "$FW/dist/py" && pwd)"
RESOURCES_DIR="$PWD/resources/python-runtime"
mkdir -p "$RESOURCES_DIR"

echo "[prepare-sidecar] building portable Python runtime (target=$TARGET)"
python3 "$FW/packages/sidecar/build/build_sidecar.py" \
  --target "$TARGET" \
  --from-wheels "$WHEELS_ABS" \
  "${EXTRA_BUILD_ARGS[@]}"

echo "[prepare-sidecar] copying runtime into resources/"
SRC="$FW/packages/sidecar/dist/python-runtime"
rm -rf "$RESOURCES_DIR" && mkdir -p "$RESOURCES_DIR"
# Skip the _cache/ directory — only contains the upstream tar.gz downloads.
for entry in "$SRC"/*; do
  name="$(basename "$entry")"
  if [[ "$name" == "_cache" ]]; then continue; fi
  cp -R "$entry" "$RESOURCES_DIR/$name"
done

echo "[prepare-sidecar] done — runtime at $RESOURCES_DIR/<platform>/"
ls -lh "$RESOURCES_DIR"
