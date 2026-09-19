"""Freeze sidecar JSON-RPC method names for the Rust binary drop-in.

The desktop host speaks these methods over stdio. Adding or renaming a
register() call without updating the catalog is a wire break.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
CATALOG_PATH = REPO_ROOT / "docs" / "spec" / "coreloop-rust-test-catalog.json"
SIDECAR_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "steerable_sidecar"
    / "sidecar.py"
)


def test_registered_rpc_methods_match_catalog() -> None:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    expected = set(catalog["sidecarRpcMethods"])
    source = SIDECAR_PATH.read_text(encoding="utf-8")
    registered = set(re.findall(r'register\("([a-z][a-z0-9_.]+)"', source))
    assert registered == expected, (
        f"added={sorted(registered - expected)} "
        f"removed={sorted(expected - registered)}"
    )
