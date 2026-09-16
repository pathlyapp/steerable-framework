#!/usr/bin/env bash
# Build the Steerable framework wheels we need to embed in the sidecar bundle.
# agent-shell 打包 helper（3.4 从 deeppath-agent 上提）。
#
# Runs the framework's release build script in-place. Idempotent — if the
# wheels already exist and you only want to refresh, run it again.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FW="${STEERABLE_FRAMEWORK_DIR:-$(cd "$SCRIPT_DIR/../../../.." && pwd)}"

if [[ ! -d "$FW/packages" ]]; then
  echo "ERROR: $FW does not look like a steerable-framework checkout." >&2
  echo "Set STEERABLE_FRAMEWORK_DIR or place the framework at $(realpath "$FW")." >&2
  exit 1
fi

if [[ ! -x "$FW/scripts/release/build-local-artifacts.sh" ]]; then
  echo "ERROR: framework is missing scripts/release/build-local-artifacts.sh — update it." >&2
  exit 1
fi

echo "[prepare-framework-wheels] building wheels in $FW"
( cd "$FW" && ./scripts/release/build-local-artifacts.sh )

echo "[prepare-framework-wheels] artifacts ready at:"
ls -1 "$FW"/dist/py/steerable_*.whl
