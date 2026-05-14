#!/usr/bin/env python3
from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    dirty = [line for line in result.stdout.splitlines() if "generated.py" in line]
    if dirty:
        print("Generated Python files are out of date:")
        for line in dirty:
            print(line)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
