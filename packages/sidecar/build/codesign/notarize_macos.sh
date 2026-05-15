#!/usr/bin/env bash
# Notarize a signed runtime via notarytool, then staple.
#
# Required env:
#   APPLE_ID                       - Apple ID
#   APPLE_TEAM_ID                  - 10-char team id
#   APPLE_APP_SPECIFIC_PASSWORD    - app-specific password
#
# Usage: notarize_macos.sh <runtime-root>

set -euo pipefail

RUNTIME_ROOT="${1:?usage: notarize_macos.sh <runtime-root>}"
APPLE_ID="${APPLE_ID:?APPLE_ID env required}"
APPLE_TEAM_ID="${APPLE_TEAM_ID:?APPLE_TEAM_ID env required}"
APPLE_APP_SPECIFIC_PASSWORD="${APPLE_APP_SPECIFIC_PASSWORD:?APPLE_APP_SPECIFIC_PASSWORD env required}"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

ZIP="$WORKDIR/runtime.zip"
echo "[notarize] zipping runtime"
ditto -c -k --keepParent "$RUNTIME_ROOT" "$ZIP"

echo "[notarize] submitting to notarytool"
xcrun notarytool submit "$ZIP" \
  --apple-id "$APPLE_ID" \
  --team-id "$APPLE_TEAM_ID" \
  --password "$APPLE_APP_SPECIFIC_PASSWORD" \
  --wait \
  --output-format json | tee "$WORKDIR/result.json"

STATUS="$(jq -r '.status' < "$WORKDIR/result.json")"
if [[ "$STATUS" != "Accepted" ]]; then
  echo "notarization failed: $STATUS" >&2
  exit 1
fi

echo "[notarize] stapling"
# Staple to every Mach-O target individually (notarytool only validates the zip).
find "$RUNTIME_ROOT" -type f \( -perm -u+x -o -name '*.dylib' -o -name '*.so' \) -print0 |
  xargs -0 -I{} bash -c 'file "$1" | grep -q "Mach-O" && xcrun stapler staple "$1" || true' _ {}
