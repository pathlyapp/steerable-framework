#!/usr/bin/env bash
# Sign every Mach-O binary inside a portable Python runtime directory.
#
# Required env:
#   MACOS_SIGN_IDENTITY    - "Developer ID Application: ..." identity to use.
#   MACOS_KEYCHAIN_PROFILE - optional keychain profile (already imported).
#
# Usage: sign_macos.sh <runtime-root>

set -euo pipefail

RUNTIME_ROOT="${1:?usage: sign_macos.sh <runtime-root>}"
IDENTITY="${MACOS_SIGN_IDENTITY:?MACOS_SIGN_IDENTITY env required}"
ENTITLEMENTS_PATH="${MACOS_ENTITLEMENTS_PATH:-}"

if [[ ! -d "$RUNTIME_ROOT" ]]; then
  echo "runtime root does not exist: $RUNTIME_ROOT" >&2
  exit 1
fi

# Find every signable binary (Mach-O object).
mapfile -t TARGETS < <(find "$RUNTIME_ROOT" \
  -type f \( -perm -u+x -o -name '*.dylib' -o -name '*.so' \) \
  -print0 | xargs -0 file --separator='|' | \
  awk -F'|' '/Mach-O/{print $1}')

echo "[codesign] discovered ${#TARGETS[@]} Mach-O binaries"

# Sign deepest-first so framework binaries finish before their containers.
for target in $(printf '%s\n' "${TARGETS[@]}" | awk '{ print length, $0 }' | sort -rn | cut -d' ' -f2-); do
  echo "  + $target"
  args=(--force --options runtime --timestamp --sign "$IDENTITY")
  if [[ -n "$ENTITLEMENTS_PATH" ]]; then
    args+=(--entitlements "$ENTITLEMENTS_PATH")
  fi
  codesign "${args[@]}" "$target"
done

echo "[codesign] verifying signatures"
for target in "${TARGETS[@]}"; do
  codesign --verify --strict --verbose=2 "$target"
done
