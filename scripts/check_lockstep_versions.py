#!/usr/bin/env python3
"""Verify all publishable Steerable packages report the same version (lockstep).

Used by CI in two ways:
  * `python scripts/check_lockstep_versions.py`
      Verify all 7 packages have the same version (any version).
      Cheap pre-commit / pre-push gate.
  * `python scripts/check_lockstep_versions.py --expected 0.3.0`
      Verify all 7 packages report exactly 0.3.0.
      Run by `release.yml` against the pushed tag — refuses to publish
      unless the source tree matches the tag.

Lockstep is enforced because the release flow is tag-driven: pushing
`v0.3.0` publishes ALL packages at 0.3.0. If even one is out of sync the
registry would land a corrupted release.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover — py3.10 path
    import tomli as tomllib  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]

TS_PACKAGES: list[tuple[str, str]] = [
    ("@steerable/agent-protocol", "packages/agent-protocol/ts/package.json"),
    ("@steerable/agent-harness",  "packages/agent-harness/ts/package.json"),
    ("@steerable/agent-ui",       "packages/agent-ui/ts/package.json"),
]

PY_PACKAGES: list[tuple[str, str]] = [
    ("steerable-agent-protocol", "packages/agent-protocol/py/pyproject.toml"),
    ("steerable-agent-harness",  "packages/agent-harness/py/pyproject.toml"),
    ("steerable-agent-runtime",  "packages/agent-runtime/py/pyproject.toml"),
    ("steerable-sidecar",        "packages/sidecar/py/pyproject.toml"),
]


def _read_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name, rel in TS_PACKAGES:
        versions[name] = json.loads((ROOT / rel).read_text())["version"]
    for name, rel in PY_PACKAGES:
        versions[name] = tomllib.loads((ROOT / rel).read_text())["project"]["version"]
    return versions


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--expected",
        help="Required version that all packages must report (e.g. '0.3.0').",
    )
    args = p.parse_args()

    versions = _read_versions()
    width = max(len(n) for n in versions)
    for name, v in versions.items():
        print(f"  {name:<{width}}  {v}")
    # Flush stdout so the version table reliably appears BEFORE any
    # error written to stderr below (otherwise terminals interleave them
    # and the error lands above the table that explains it).
    sys.stdout.flush()

    distinct = set(versions.values())
    if len(distinct) != 1:
        print(
            f"\nERROR: lockstep mismatch — packages report {len(distinct)} distinct "
            f"versions: {sorted(distinct)}",
            file=sys.stderr,
        )
        return 1

    only_version = distinct.pop()
    if args.expected and args.expected != only_version:
        print(
            f"\nERROR: expected version {args.expected!r} but all packages "
            f"report {only_version!r}",
            file=sys.stderr,
        )
        return 1

    suffix = " (matches --expected)" if args.expected else ""
    print(f"\nOK: all 7 packages at {only_version}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
