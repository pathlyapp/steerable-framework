"""Structured search primitives for the ``grep`` / ``glob`` tools (W1.4.1).

The one-shot ``bash grep -rn`` path returns unstructured text clipped at
32 KB — one large-repo search costs thousands of tokens, and a shell-quoting
mistake costs a retry round. These primitives return structured hits
(``{path, line, text}``) with explicit limits; the same information costs a
third to a fifth of the tokens.

Traversal is shared between grep and glob and applies the ignore set —
``ls -R`` drowning in ``node_modules`` is the concrete disease glob cures.

``rg`` is preferred when present (fast on large repos); a pure-Python
fallback keeps the tools working in bare task containers. Both paths return
identical hit shapes so the model cannot tell which ran.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Directories never worth an agent's tokens. Deliberately short and
#: universal — language-specific clutter (target/, .next/) is the model's
#: to navigate, but these five swamp every traversal regardless of stack.
IGNORE_DIRS = frozenset(
    {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build"}
)

#: Hard caps so a pathological repo cannot flood the transcript even when
#: the model forgets to pass a limit.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
MAX_LINE_CHARS = 500


@dataclass(frozen=True, slots=True)
class SearchHit:
    path: str
    line: int
    text: str


def _walk_files(root: Path) -> list[Path]:
    """All files under root, ignore set applied, deterministic order."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS)
        for name in sorted(filenames):
            out.append(Path(dirpath) / name)
    return out


def glob_files(root: Path, pattern: str, *, limit: int = DEFAULT_LIMIT) -> list[str]:
    """Repo-relative paths matching a fnmatch pattern, ignore set applied."""
    if not pattern or not pattern.strip():
        raise ValueError("pattern is empty")
    limit = max(1, min(limit, MAX_LIMIT))
    matches: list[str] = []
    for path in _walk_files(root):
        rel = path.relative_to(root).as_posix()
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(path.name, pattern):
            matches.append(rel)
            if len(matches) >= limit:
                break
    return matches


def search(
    root: Path,
    query: str,
    *,
    is_regex: bool = False,
    ignore_case: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> list[SearchHit]:
    """Structured content search; rg when available, pure Python otherwise."""
    if not query:
        raise ValueError("query is empty")
    limit = max(1, min(limit, MAX_LIMIT))
    if is_regex:
        # Validate before choosing a backend: rg exits non-zero on a bad
        # pattern without raising, so the check must live here to fail loud.
        try:
            re.compile(query)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
    if shutil.which("rg"):
        try:
            return _search_rg(root, query, is_regex=is_regex, ignore_case=ignore_case, limit=limit)
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
            # rg present but failed (flags, encoding) — fall through to the
            # portable path rather than strand the tool on one binary.
            pass
    return _search_python(root, query, is_regex=is_regex, ignore_case=ignore_case, limit=limit)


def _search_rg(
    root: Path, query: str, *, is_regex: bool, ignore_case: bool, limit: int
) -> list[SearchHit]:
    argv = ["rg", "--json", "--max-count", str(limit)]
    if not is_regex:
        argv.append("--fixed-strings")
    if ignore_case:
        argv.append("--ignore-case")
    for ignored in IGNORE_DIRS:
        # Root-anchored `!node_modules/**` misses when the search root is an
        # absolute path; the `**/` prefix anchors at any depth.
        argv.extend(["--glob", f"!**/{ignored}/**"])
    argv.extend(["--", query, str(root)])
    proc = subprocess.run(
        argv, check=False, capture_output=True, text=True, timeout=30
    )
    hits: list[SearchHit] = []
    for line in proc.stdout.splitlines():
        event = json.loads(line)
        if event.get("type") != "match":
            continue
        data = event["data"]
        text = data["lines"]["text"].rstrip("\n")
        hits.append(
            SearchHit(
                path=str(Path(data["path"]["text"]).relative_to(root)),
                line=int(data["line_number"]),
                text=text[:MAX_LINE_CHARS],
            )
        )
        if len(hits) >= limit:
            break
    return hits


def _search_python(
    root: Path, query: str, *, is_regex: bool, ignore_case: bool, limit: int
) -> list[SearchHit]:
    if is_regex:
        flags = re.IGNORECASE if ignore_case else 0
        try:
            pattern = re.compile(query, flags)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
    else:
        needle = query.lower() if ignore_case else query
        pattern = None

    hits: list[SearchHit] = []
    for path in _walk_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern is not None:
                matched = pattern.search(line) is not None
            else:
                haystack = line.lower() if ignore_case else line
                matched = needle in haystack
            if matched:
                hits.append(
                    SearchHit(
                        path=path.relative_to(root).as_posix(),
                        line=lineno,
                        text=line[:MAX_LINE_CHARS],
                    )
                )
                if len(hits) >= limit:
                    return hits
    return hits
