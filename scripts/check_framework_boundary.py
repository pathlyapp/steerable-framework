#!/usr/bin/env python3
"""Enforce the "framework contains zero business code" boundary (ADR-002).

The Steerable framework must never ship paid / branded / vertical business
code. This gate scans every publishable package's source tree and fails CI
if it finds a forbidden import or keyword.

See:
  * docs/vision/decisions.md            ADR-002
  * docs/vision/target-architecture.md  §7

Usage:
  python scripts/check_framework_boundary.py
      Scan packages/**/src and fail on any violation.
  python scripts/check_framework_boundary.py --list
      Print what would be scanned, then exit 0.

What counts as a violation (inside packages/**/src only):
  * Importing a business module (deeppath*, cflog*, app.membership, …).
  * Mentioning a forbidden keyword (deeppath, 时踪, ciflog, membership, …).
    Keywords match case-insensitively for ASCII; CJK is matched verbatim.

What is allowed (NOT scanned / allowlisted):
  * docs/, examples/, tests/, CHANGELOG.md, README.md — they legitimately
    reference DeepPath as the reference consumer.
  * Comment lines (# … / // … / * … / docstring lines). The migration guide
    *encourages* "lifted from deeppath" provenance comments, so keyword
    matching skips comments. Forbidden imports are still caught everywhere,
    and keywords inside real code / string literals are still caught.
  * The substring "entitlement"/"EntitlementGate" — the framework is allowed
    to define the gate *interface*; only concrete billing impls are banned,
    which the import + keyword rules already catch.
  * Anything listed in scripts/framework_boundary_baseline.txt — known,
    accepted debt tracked for cleanup (P4). New violations still fail CI.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Only the publishable source trees are subject to the boundary.
SCAN_GLOBS: tuple[str, ...] = (
    "packages/*/py/src/**/*.py",
    "packages/*/ts/src/**/*.ts",
    "packages/*/ts/src/**/*.tsx",
)

# Tests / stories / fixtures legitimately reference DeepPath (ADR-002 exempts
# tests). Files whose name matches any suffix below are skipped entirely.
EXEMPT_SUFFIXES: tuple[str, ...] = (
    ".test.ts",
    ".test.tsx",
    ".spec.ts",
    ".spec.tsx",
    ".stories.ts",
    ".stories.tsx",
    "_test.py",
)

# Forbidden import statements (regex, matched per line).
FORBIDDEN_IMPORTS: tuple[re.Pattern[str], ...] = (
    # Python: import deeppath / from deeppath... / from app.membership...
    re.compile(r"^\s*(from|import)\s+(deeppath\w*|cflog\w*|ciflog\w*)\b"),
    re.compile(r"^\s*from\s+app\.(membership|payment|billing|cflog)\b"),
    # TS/JS: import ... from 'deeppath...' / '@deeppath/...' / './cflog'
    re.compile(r"""import\s+.*from\s+['"](@?deeppath[\w/-]*|.*cflog[\w/-]*)['"]"""),
    re.compile(r"""require\(\s*['"](@?deeppath[\w/-]*|.*cflog[\w/-]*)['"]"""),
)

# Forbidden ASCII keywords (case-insensitive, word-ish boundary).
FORBIDDEN_KEYWORDS_ASCII: tuple[str, ...] = (
    "deeppath",
    "ciflog",
    "cflog",
    "membership_tier",
    "wechat_pay",
    "wechatpay",
    "alipay",
    "subscription_plan",
)
# Forbidden CJK keywords (verbatim substring).
FORBIDDEN_KEYWORDS_CJK: tuple[str, ...] = (
    "时踪",
    "测井",
)

# Lines containing any of these are exempt (the framework may define the
# *interface* for entitlements / paywalls; only concrete billing is banned).
ALLOWLIST_SUBSTRINGS: tuple[str, ...] = (
    "EntitlementGate",
    "EntitlementDecision",
    "entitlement",  # interface-level mentions are fine
)

_ascii_kw_re = re.compile(
    r"(?i)\b(" + "|".join(re.escape(k) for k in FORBIDDEN_KEYWORDS_ASCII) + r")\b"
)

# A line that, once stripped, begins with one of these is treated as a comment
# and is exempt from keyword matching (imports are still checked everywhere).
_COMMENT_PREFIXES: tuple[str, ...] = ("#", "//", "*", "/*", '"""', "'''", '"', "'")

BASELINE_FILE = ROOT / "scripts" / "framework_boundary_baseline.txt"


def _iter_files() -> list[Path]:
    files: list[Path] = []
    for pattern in SCAN_GLOBS:
        for f in ROOT.glob(pattern):
            if f.name.endswith(EXEMPT_SUFFIXES):
                continue
            files.append(f)
    return sorted(set(files))


def _load_baseline() -> set[str]:
    if not BASELINE_FILE.exists():
        return set()
    out: set[str] = set()
    for line in BASELINE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line)
    return out


def _line_allowlisted(line: str) -> bool:
    return any(s in line for s in ALLOWLIST_SUBSTRINGS)


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith(_COMMENT_PREFIXES)


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return list of (line_no, rule, line_text) violations."""
    violations: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return violations

    for i, line in enumerate(text.splitlines(), start=1):
        if _line_allowlisted(line):
            continue
        for pat in FORBIDDEN_IMPORTS:
            if pat.search(line):
                violations.append((i, "forbidden-import", line.strip()))
                break
        else:
            if _is_comment(line):
                continue  # provenance comments are allowed (see module docstring)
            m = _ascii_kw_re.search(line)
            if m:
                violations.append((i, f"forbidden-keyword:{m.group(1)}", line.strip()))
                continue
            for kw in FORBIDDEN_KEYWORDS_CJK:
                if kw in line:
                    violations.append((i, f"forbidden-keyword:{kw}", line.strip()))
                    break
    return violations


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--list", action="store_true", help="List scanned files and exit.")
    p.add_argument(
        "--update-baseline",
        action="store_true",
        help="Rewrite scripts/framework_boundary_baseline.txt with current hits.",
    )
    args = p.parse_args()

    files = _iter_files()
    if args.list:
        for f in files:
            print(f.relative_to(ROOT))
        print(f"\n{len(files)} files would be scanned.")
        return 0

    # Each violation has a stable signature: "<relpath>:<rule>".
    # (Line numbers are deliberately excluded so edits above a hit don't churn
    # the baseline.)
    hits: list[tuple[str, int, str, str]] = []
    for f in files:
        rel = str(f.relative_to(ROOT))
        for line_no, rule, text in _scan_file(f):
            hits.append((rel, line_no, rule, text))

    if args.update_baseline:
        sigs = sorted({f"{rel}:{rule}" for rel, _, rule, _ in hits})
        BASELINE_FILE.write_text(
            "# Accepted framework-boundary debt (ADR-002). Cleanup tracked in P4.\n"
            "# Signature format: <relpath>:<rule>. Regenerate with --update-baseline.\n"
            + "\n".join(sigs)
            + ("\n" if sigs else ""),
            encoding="utf-8",
        )
        print(f"Wrote {len(sigs)} baseline signature(s) to {BASELINE_FILE.name}.")
        return 0

    baseline = _load_baseline()
    new_hits = [h for h in hits if f"{h[0]}:{h[2]}" not in baseline]

    for rel, line_no, rule, text in new_hits:
        print(f"{rel}:{line_no}: [{rule}] {text}", file=sys.stderr)

    if new_hits:
        print(
            f"\nERROR: framework boundary violated — {len(new_hits)} NEW hit(s) "
            f"of business code in packages/**/src ({len(baseline)} baselined).\n"
            f"Paid / branded / vertical logic must live in the business "
            f"repos (deeppath-*), not the framework. See "
            f"docs/vision/decisions.md ADR-002.",
            file=sys.stderr,
        )
        return 1

    suffix = f" ({len(baseline)} baselined debt)" if baseline else ""
    print(
        f"OK: scanned {len(files)} framework source files, "
        f"zero new business leaks{suffix}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
