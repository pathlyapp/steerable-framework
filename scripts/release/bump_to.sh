#!/usr/bin/env bash
# Bump every publishable Steerable package to the same target version
# (lockstep). Run BEFORE tagging:
#
#   ./scripts/release/bump_to.sh 0.3.0
#   git diff                                        # eyeball it
#   git add -A && git commit -m "chore(release): v0.3.0"
#   git tag v0.3.0
#   git push origin main v0.3.0
#
# CI (`.github/workflows/release.yml`) takes over from the tag push: it
# validates the lockstep, creates the GitHub Release, and chains
# publish-{npm,pypi}.yml. Those are idempotent — anything already on the
# registry is skipped — so a re-tag after a botched run is safe.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 X.Y.Z   (semver, no leading 'v', e.g. 0.3.0 or 0.3.0-rc.1)" >&2
  exit 1
fi
VERSION="$1"

# Loose semver: MAJOR.MINOR.PATCH with optional prerelease/build suffix.
# Strict enough to catch typos like "0.3" or "v0.3.0".
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.-]+)?(\+[A-Za-z0-9.-]+)?$ ]]; then
  echo "ERROR: '$VERSION' is not a valid semver string." >&2
  exit 1
fi

cd "$(dirname "$0")/../.."

if [[ -n "$(git status --porcelain)" ]]; then
  echo "ERROR: working tree has uncommitted changes — commit or stash before bumping." >&2
  git status --short
  exit 1
fi

VERSION="$VERSION" python3 - <<'PY'
import json, os, re
from pathlib import Path

version = os.environ["VERSION"]

ts_pkgs = [
    "packages/agent-protocol/ts",
    "packages/agent-harness/ts",
    "packages/agent-ui/ts",
]
py_pkgs = [
    "packages/agent-protocol/py",
    "packages/agent-harness/py",
    "packages/agent-runtime/py",
    "packages/sidecar/py",
]

print(f"\nBumping all 7 packages to {version}:\n")

for d in ts_pkgs:
    p = Path(d) / "package.json"
    data = json.loads(p.read_text())
    old = data["version"]
    data["version"] = version
    # `json.dumps` with indent=2 + trailing newline mirrors the format
    # release-please used to write, so diffs stay minimal.
    p.write_text(json.dumps(data, indent=2) + "\n")
    print(f"  {data['name']:<32}  {old:<10} -> {version}")

# pyproject.toml: surgical regex on the `version = "..."` line under
# `[project]` only — never touch dependency pins, prerelease specifiers,
# or comments containing version-like strings.
project_version_re = re.compile(
    r'(\[project\][\s\S]*?\nversion\s*=\s*")([^"]+)(")',
    re.MULTILINE,
)
project_name_re = re.compile(r'\[project\][\s\S]*?\nname\s*=\s*"([^"]+)"', re.MULTILINE)

for d in py_pkgs:
    p = Path(d) / "pyproject.toml"
    text = p.read_text()
    m = project_version_re.search(text)
    if not m:
        raise SystemExit(f"ERROR: could not locate '[project] / version = ...' in {p}")
    old = m.group(2)
    text = text[:m.start(2)] + version + text[m.end(2):]
    p.write_text(text)
    name = project_name_re.search(text).group(1)
    print(f"  {name:<32}  {old:<10} -> {version}")
PY

echo
echo "Refreshing lockfiles..."
pnpm install --lockfile-only
uv lock

echo
echo "Lockstep sanity check:"
uv run python scripts/check_lockstep_versions.py --expected "$VERSION"

echo
echo "Done. Next steps:"
echo "  git diff                                        # review"
echo "  git add -A && git commit -m 'chore(release): v$VERSION'"
echo "  git tag v$VERSION"
echo "  git push origin main v$VERSION                   # CI publishes from here"
