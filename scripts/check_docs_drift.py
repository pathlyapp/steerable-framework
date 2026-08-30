#!/usr/bin/env python3
"""Docs-vs-code drift gate.

User-facing docs make claims about framework capabilities; when the code
stops backing a claim (or a doc still denies code that now exists), the doc
becomes a lie that costs credibility. Each check below pairs a doc assertion
with the source evidence that must hold. A failure prints every broken rule
and exits 1.

Wire next to ``check_drift.py`` in ci.yml. Keep rules narrow and explicit —
this gate guards specific past regressions, not general prose quality.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

RUNTIME_SRC = "packages/agent-runtime/py/src/steerable_agent_runtime"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def check_core_loop_status() -> str | None:
    """core-loop.md must not deny an implementation that exists."""
    doc = _read("docs/spec/core-loop.md")
    code = _read(f"{RUNTIME_SRC}/loop.py")
    if "class CoreLoop" in code and "no implementation yet" in doc:
        return (
            "docs/spec/core-loop.md says 'no implementation yet' but "
            f"{RUNTIME_SRC}/loop.py implements CoreLoop — update the status header."
        )
    return None


def check_readme_schema_claim() -> str | None:
    """README's auto-derived-schema claim requires working derivation."""
    readme = _read("README.md")
    if "auto-derive" not in readme and "auto-derived" not in readme:
        return None  # claim dropped; nothing to verify
    try:
        module = _read(f"{RUNTIME_SRC}/tool_schema.py")
    except FileNotFoundError:
        return (
            "README claims auto-derived JSON Schema from type hints but "
            f"{RUNTIME_SRC}/tool_schema.py does not exist."
        )
    if "def derive_schema" not in module:
        return (
            "README claims auto-derived JSON Schema but tool_schema.py "
            "has no derive_schema()."
        )
    if "derive_schema" not in _read(f"{RUNTIME_SRC}/tools.py"):
        return (
            "README claims auto-derived JSON Schema but ToolRouter never "
            "calls derive_schema — the claim is not wired to reality."
        )
    return None


CHECKS = [check_core_loop_status, check_readme_schema_claim]


def main() -> int:
    failures = [msg for check in CHECKS if (msg := check()) is not None]
    for msg in failures:
        print(f"docs drift: {msg}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
