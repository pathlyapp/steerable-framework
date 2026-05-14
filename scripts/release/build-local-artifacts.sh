#!/usr/bin/env bash
# Build all publishable artifacts (npm tarballs + Py wheels) into ./dist/.
# See RELEASING.md for the full workflow.
set -euo pipefail

cd "$(dirname "$0")/../.."
ROOT="$(pwd)"

echo "== 1/5  pnpm install + uv sync"
pnpm install --frozen-lockfile
uv sync --all-packages

echo "== 2/5  codegen"
# We deliberately skip pnpm check:drift / scripts/check_drift.py here: drift
# checks are CI invariants that compare freshly-generated output against the
# committed tree. After running `pnpm gen`/`generate_py.py` locally there is
# always uncommitted diff, which is exactly what we want — bake it into the
# tarballs. CI runs the drift check separately on a clean tree.
pnpm gen
uv run python scripts/generate_py.py

echo "== 3/5  build TS"
pnpm -r --filter '@steerable/*' build

echo "== 4/5  pack TS tarballs + build Py wheels"
rm -rf dist
mkdir -p dist/npm dist/py
for pkg in agent-protocol agent-harness agent-ui; do
  (cd "packages/$pkg/ts" && \
   pnpm pack --pack-destination "$ROOT/dist/npm" >/dev/null)
  echo "    packed @steerable/$pkg"
done
uv build --all-packages --out-dir dist/py >/dev/null
rm -f dist/py/steerable_example_*.whl dist/py/steerable_example_*.tar.gz
echo "    built $(ls dist/py | wc -l | tr -d ' ') Python files"

echo "== 5/5  artifact summary"
echo "  TS tarballs ($(ls dist/npm | wc -l | tr -d ' ')):"
ls -lh dist/npm | tail -n +2 | awk '{printf "    %s  %s\n", $5, $NF}'
echo "  Py wheels + sdists ($(ls dist/py | wc -l | tr -d ' ')):"
ls -lh dist/py | tail -n +2 | awk '{printf "    %s  %s\n", $5, $NF}'

echo
echo "Done. Run ./scripts/release/verify-local-artifacts.sh to smoke-test."
