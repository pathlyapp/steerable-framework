#!/usr/bin/env bash
# Generate Linux VRT baselines for @steerable/agent-ui using the official
# Playwright Docker image.
#
# Why: Storybook VRT snapshots are platform-sensitive (font hinting,
# antialiasing). Local darwin baselines satisfy `pnpm storybook:vrt` on macOS
# but the CI runner (Linux) needs its own set under
# `tests/visual/__screenshots__/*-chromium-desktop-linux.png`. This script
# spins up the same Playwright container CI uses and updates the snapshots
# in place — commit the resulting `*-linux.png` files alongside your change.
#
# Usage:
#   scripts/generate-vrt-baselines-linux.sh
#
# Requires Docker. Tested on Apple Silicon (arm64) and x86_64.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG_DIR="packages/agent-ui/ts"
PLAYWRIGHT_VERSION="$(node -p "require('${ROOT}/${PKG_DIR}/node_modules/@playwright/test/package.json').version")"

echo "== Building Storybook bundle (host) =="
pnpm --filter @steerable/agent-ui storybook:build

echo "== Generating Linux baselines via Playwright Docker (v${PLAYWRIGHT_VERSION}) =="
docker run --rm --ipc=host \
  -v "${ROOT}":/work \
  -w /work \
  --entrypoint /bin/bash \
  "mcr.microsoft.com/playwright:v${PLAYWRIGHT_VERSION}-jammy" \
  -c "
    set -euo pipefail
    corepack enable
    cd ${PKG_DIR}
    pnpm install --frozen-lockfile --ignore-scripts
    pnpm storybook:vrt:update
  "

echo
echo "Done. Inspect & commit:"
echo "  git status -- ${PKG_DIR}/tests/visual/__screenshots__"
