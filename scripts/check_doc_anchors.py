#!/usr/bin/env python3
"""Negative-capability anchor gate.

Docs that claim a capability is absent ("unimplemented", "deliberately
deferred", "计划中", "no Windows rewriter", …) drift silently when the code
later ships that capability — twice now this repo's docs understated shipped
work for a full review round. This gate requires every such claim to carry a
checkable anchor and fails when the anchor's evidence appears in the code.

Anchor syntax, on the claim line or the line directly above it:

    <!-- anchor: path/relative/to/repo-root.py :: REGEX -->

(inside Python docstrings the same ``anchor: path :: REGEX`` text without the
comment markers). The claim is stale — and the gate fails — when REGEX
matches the target file. A trigger line with no anchor also fails.

Wire next to ``check_docs_drift.py`` in ci.yml. Trigger phrases are the
fixed vocabulary below; add a phrase only with a matching doc convention.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCAN_GLOBS = ["docs/**/*.md", "*.md", "packages/*/py/src/**/*.py"]

# Phrases whose presence on a line makes a negative-capability claim.
TRIGGERS = re.compile(
    r"未实现|刻意推迟|计划中|尚未(?:实现|接线)"
    r"|仅\s*(?:macOS|Linux|Windows)"
    r"|\bunimplemented\b|\bnot implemented\b|\bno implementation yet\b"
    r"|\bdeliberately (?:deferred|unimplemented)\b"
    r"|\b(?:is|are) planned\b"
    r"|\bno (?:Windows|macOS|Linux) rewriter\b|\bhas no rewriter\b",
    re.IGNORECASE,
)

ANCHOR = re.compile(r"anchor:\s*([\w./-]+)\s*::\s*(\S(?:.*?\S)?)\s*(?:-->)?\s*$")

# Quoted spans are protocol strings or grep patterns, not claims: HTTP
# ``"Not Implemented"`` is a permanent status name, and a backticked
# `no implementation yet` in a TODO quotes the text to grep for. Single
# quotes are not stripped — prose apostrophes make them ambiguous.
QUOTED = re.compile(r'"[^"]*"|`[^`]*`|「[^」]*`|“[^”]*”')


def _strip_quoted(line: str) -> str:
    return QUOTED.sub("", line)


# Table header rows like `| 已实现 | 未实现 |` name columns, not claims.
def _is_table_header(line: str) -> bool:
    return line.lstrip().startswith("|") and "已实现" in line and "未实现" in line


def _iter_lines() -> tuple[list[tuple[Path, int, str]], list[str]]:
    """Collect (path, lineno, line) plus non-fatal scan errors."""
    lines: list[tuple[Path, int, str]] = []
    errors: list[str] = []
    seen: set[Path] = set()
    for pattern in SCAN_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                errors.append(f"{path.relative_to(ROOT)}: not utf-8, skipped")
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                lines.append((path, lineno, line))
    return lines, errors


def check() -> list[str]:
    lines, failures = _iter_lines()
    by_file: dict[Path, list[str]] = {}
    for path, _lineno, line in lines:
        by_file.setdefault(path, []).append(line)

    for path, lineno, line in lines:
        if _is_table_header(line) or not TRIGGERS.search(_strip_quoted(line)):
            continue
        rel = path.relative_to(ROOT)
        candidates = [line]
        if lineno > 1:
            candidates.append(by_file[path][lineno - 2])
        anchor = next(
            (m for cand in candidates if (m := ANCHOR.search(cand))), None
        )
        if anchor is None:
            failures.append(
                f"{rel}:{lineno}: negative-capability claim without an anchor; "
                "add '<!-- anchor: path :: regex -->' naming the code evidence "
                "that would disprove it"
            )
            continue
        target_rel, regex = anchor.group(1), anchor.group(2)
        target = ROOT / target_rel
        if not target.is_file():
            failures.append(f"{rel}:{lineno}: anchor target {target_rel} missing")
            continue
        try:
            pattern = re.compile(regex)
        except re.error as exc:
            failures.append(f"{rel}:{lineno}: bad anchor regex {regex!r}: {exc}")
            continue
        if pattern.search(target.read_text(encoding="utf-8")):
            failures.append(
                f"{rel}:{lineno}: claim is stale — anchor regex {regex!r} now "
                f"matches {target_rel}; update the doc"
            )
    return failures


def main() -> int:
    failures = check()
    for msg in failures:
        print(f"doc anchor: {msg}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
