#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def read_pkg_version(path: Path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["version"]


def read_py_version(path: Path) -> str:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return data["project"]["version"]


def main() -> int:
    protocol_ts = read_pkg_version(
        ROOT / "packages" / "agent-protocol" / "ts" / "package.json"
    )
    protocol_py = read_py_version(
        ROOT / "packages" / "agent-protocol" / "py" / "pyproject.toml"
    )
    harness_ts = read_pkg_version(
        ROOT / "packages" / "agent-harness" / "ts" / "package.json"
    )
    harness_py = read_py_version(
        ROOT / "packages" / "agent-harness" / "py" / "pyproject.toml"
    )
    if protocol_ts != protocol_py:
        raise SystemExit(
            f"Lockstep mismatch for protocol: ts={protocol_ts}, py={protocol_py}"
        )
    if harness_ts != harness_py:
        raise SystemExit(
            f"Lockstep mismatch for harness: ts={harness_ts}, py={harness_py}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
