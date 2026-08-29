"""Structured file editing engine (W6-1), mirrored from the desktop's
``local-edit.ts`` so the headless / ACP surface has parity with the product.

Semantics synthesise the strongest subset of the three peer frameworks:

* codex ``apply-patch`` ``seek_sequence`` — three-level degraded location
  (exact → whitespace-trimmed → Unicode-punctuation-normalised).
* pi ``edit-diff`` — batched ``edits[]``: every edit is located against the
  *original* content first, then applied in reverse document order with
  overlap rejected.
* dsh read-before-write — optional ``expected_version`` conflict token
  (SHA-256 of the content) enforced by the caller in ``workspace_tools``.

This module is pure string surgery: no filesystem, no I/O. The tool wrapper
owns reading, version checking, atomic write, and per-path serialisation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

#: Length-preserving Unicode punctuation normalisation (1 char → 1 char) so a
#: match offset maps straight back onto the original text. Only the common
#: confusables where a model's anchor and the file's bytes most often differ.
_UNICODE_PUNCT = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "—": "-", "–": "-", "―": "-",
    "…": ".", "·": ".",
    " ": " ",
}


def content_version(content: str) -> str:
    """Read-before-write conflict token: SHA-256 of the UTF-8 content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class EditError(Exception):
    """Structured edit failure with a machine-readable ``code``."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class EditOp:
    old_text: str
    new_text: str


@dataclass(slots=True)
class AppliedEdit:
    index: int
    length: int
    level: str  # "exact" | "trim" | "unicode"
    start_line: int
    old_line_count: int
    new_lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ApplyResult:
    content: str
    matches: list[AppliedEdit]
    diff: str


def _normalise_unicode(text: str) -> str:
    return "".join(_UNICODE_PUNCT.get(ch, ch) for ch in text)


def _find_exact(content: str, old: str) -> tuple[int, int] | str | None:
    first = content.find(old)
    if first == -1:
        return None
    if content.find(old, first + 1) != -1:
        return "ambiguous"
    return (first, len(old))


def _find_trim(content: str, old: str) -> tuple[int, int] | str | None:
    old_lines = [line.strip() for line in old.split("\n")]
    if not old_lines or (len(old_lines) == 1 and old_lines[0] == ""):
        return None

    # Line [start, end) offsets for content (end excludes the newline).
    line_starts = [0]
    for i, ch in enumerate(content):
        if ch == "\n":
            line_starts.append(i + 1)
    line_count = len(line_starts)

    def line_end(line: int) -> int:
        start = line_starts[line]
        nxt = line_starts[line + 1] if line + 1 < line_count else len(content)
        end = nxt
        if end > start and content[end - 1] == "\n":
            end -= 1
        return end

    trimmed = [content[line_starts[l]:line_end(l)].strip() for l in range(line_count)]
    window = len(old_lines)
    if window > line_count:
        return None
    hits: list[int] = []
    for start in range(0, line_count - window + 1):
        if trimmed[start:start + window] == old_lines:
            hits.append(start)
    if not hits:
        return None
    if len(hits) > 1:
        return "ambiguous"
    start_line = hits[0]
    index = line_starts[start_line]
    end = line_end(start_line + window - 1)
    return (index, end - index)


def _find_unicode(content: str, old: str) -> tuple[int, int] | str | None:
    norm_content = _normalise_unicode(content)
    norm_old = _normalise_unicode(old)
    first = norm_content.find(norm_old)
    if first == -1:
        return None
    if norm_content.find(norm_old, first + 1) != -1:
        return "ambiguous"
    return (first, len(norm_old))


def _locate(content: str, old: str) -> tuple[int, int, str]:
    if not old:
        raise EditError(
            "oldText 为空——edit 用于替换已有片段；新建/整文件覆盖请用 write_file。",
            code="empty_old",
        )
    levels = (
        ("exact", _find_exact),
        ("trim", _find_trim),
        ("unicode", _find_unicode),
    )
    saw_ambiguous = False
    for level, run in levels:
        hit = run(content, old)
        if hit == "ambiguous":
            saw_ambiguous = True
            continue
        if hit:
            index, length = hit
            return (index, length, level)
    if saw_ambiguous:
        raise EditError(
            "oldText 在文件中匹配到多处——请扩大上下文（多带几行）以唯一定位。",
            code="ambiguous",
        )
    raise EditError(
        "oldText 在文件中未找到（已尝试精确 / 去空白 / Unicode 归一三级匹配）。"
        "请先重新读取该文件，确认要替换的原文与当前内容一致。",
        code="not_found",
    )


def apply_edits(content: str, edits: list[EditOp], *, file_path: str = "file") -> ApplyResult:
    """Locate every edit against the original content, reject overlap, apply
    in reverse document order, and build a unified diff of the change."""
    if not edits:
        raise EditError("edits 为空——至少提供一条 {oldText, newText}。", code="no_edits")

    matches: list[AppliedEdit] = []
    for edit in edits:
        index, length, level = _locate(content, edit.old_text)
        old_span = content[index:index + length]
        matches.append(
            AppliedEdit(
                index=index,
                length=length,
                level=level,
                start_line=content.count("\n", 0, index),
                old_line_count=old_span.count("\n") + 1,
                new_lines=edit.new_text.split("\n"),
            )
        )

    ordered = sorted(matches, key=lambda m: m.index)
    for i in range(1, len(ordered)):
        prev, cur = ordered[i - 1], ordered[i]
        if cur.index < prev.index + prev.length:
            raise EditError(
                f"第 {i + 1} 条编辑与前面的编辑在原文中区间重叠——请合并成一条，或缩小各自的 oldText。",
                code="overlap",
            )

    next_content = content
    for m, edit in sorted(
        zip(matches, edits), key=lambda pair: pair[0].index, reverse=True
    ):
        next_content = (
            next_content[: m.index] + edit.new_text + next_content[m.index + m.length:]
        )

    diff = _build_unified_diff(file_path, content, matches)
    return ApplyResult(content=next_content, matches=ordered, diff=diff)


def _build_unified_diff(
    file_path: str, original: str, matches: list[AppliedEdit], *, context: int = 3
) -> str:
    """Build a unified diff from the known edits (the file's only changes), so
    no general diff algorithm is needed; adjacent hunks merge."""
    orig_lines = original.split("\n")
    hunks = sorted(matches, key=lambda m: m.start_line)

    groups: list[dict] = []
    for m in hunks:
        hunk_end = m.start_line + m.old_line_count
        if groups and m.start_line - groups[-1]["end"] <= context * 2:
            groups[-1]["items"].append(m)
            groups[-1]["end"] = max(groups[-1]["end"], hunk_end)
        else:
            groups.append({"start": m.start_line, "end": hunk_end, "items": [m]})

    out = [f"--- a/{file_path}", f"+++ b/{file_path}"]
    net_delta = 0
    for g in groups:
        ctx_start = max(0, g["start"] - context)
        ctx_end = min(len(orig_lines), g["end"] + context)
        old_len = ctx_end - ctx_start
        removed = sum(m.old_line_count for m in g["items"])
        added = sum(len(m.new_lines) for m in g["items"])
        new_len = old_len - removed + added
        old_start = ctx_start + 1
        new_start = ctx_start + 1 + net_delta
        out.append(f"@@ -{old_start},{old_len} +{new_start},{new_len} @@")

        by_start = {m.start_line: m for m in g["items"]}
        i = ctx_start
        while i < ctx_end:
            hunk = by_start.get(i)
            if hunk is not None:
                for line in orig_lines[i:i + hunk.old_line_count]:
                    out.append(f"-{line}")
                for line in hunk.new_lines:
                    out.append(f"+{line}")
                i += hunk.old_line_count
            else:
                out.append(f" {orig_lines[i]}")
                i += 1
        net_delta += added - removed
    return "\n".join(out)
