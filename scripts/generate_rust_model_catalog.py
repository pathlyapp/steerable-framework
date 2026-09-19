#!/usr/bin/env python3
"""Project the checked-in Python model catalog into Rust-readable JSON."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (
    ROOT
    / "packages"
    / "agent-runtime"
    / "py"
    / "src"
    / "steerable_agent_runtime"
    / "model_catalog.py"
)
OUTPUT = ROOT / "packages" / "agent-runtime" / "rs" / "src" / "model_catalog.json"


def _assignments(source: str) -> dict[str, object]:
    tree = ast.parse(source)
    values: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id in {"MODEL_ENTRIES", "PROVIDER_ENTRIES"}:
            values[node.target.id] = ast.literal_eval(node.value)
    return values


def render() -> str:
    source = SOURCE.read_text(encoding="utf-8")
    values = _assignments(source)
    payload = {
        "sourceSha256": hashlib.sha256(source.encode()).hexdigest(),
        "models": values["MODEL_ENTRIES"],
        "providers": values["PROVIDER_ENTRIES"],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    artifact = render()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != artifact:
            raise SystemExit(f"catalog drift: regenerate {OUTPUT}")
        print(f"catalog up to date: {OUTPUT}")
        return 0
    OUTPUT.write_text(artifact, encoding="utf-8")
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
