"""Pseudo / markdown tool-call recovery.

Some models (notably local ones served via Ollama, and Claude/OpenAI when they
regress into prose) emit tool *intent* as text instead of a structured
``tool_calls`` block:

- MiniMax XML:      ``<invoke name="tool"><parameter name="k">v</parameter></invoke>``
- DeepSeek XML:     ``<function=tool><parameter=k>v</parameter></function>``
- Markdown pseudo:  ``[Tool call: NAME]\\n{"k": "v"}``

If the loop treated that text as a final answer it would end the turn having
never run the tool — the model "meant" to act but the loop stopped. This module
recovers those inline calls into real ``ToolCall`` objects so the act phase
runs, and returns the surrounding narration with the pseudo blocks removed.

Recovery (not just stripping) is what lets the loop drive real local models:
stripping would discard the model's intent and end the turn tool-less.

This is a whole-text extraction applied after a full round of content is
collected. Streaming-time stripping for display cleanliness lives below
(``PseudoStreamStripper``), ported from deeppath-api's production SSE path.
"""

from __future__ import annotations

import json
import re
from typing import Any

# --- XML families -----------------------------------------------------------

_INVOKE_TAG_RE = re.compile(
    r"<invoke\s+name=[\"']([^\"']+)[\"']\s*>(.*?)</invoke>",
    re.DOTALL,
)
_FUNC_TAG_RE = re.compile(
    r"<function=([^>]+)>(.*?)</function>",
    re.DOTALL,
)
_PARAM_TAG_RE = re.compile(
    r"<parameter\s+name=[\"']([^\"']+)[\"']\s*>(.*?)</parameter>",
    re.DOTALL,
)
_PARAM_EQ_TAG_RE = re.compile(
    r"<parameter=([^>]+)>(.*?)</parameter>",
    re.DOTALL,
)

# --- Markdown pseudo --------------------------------------------------------

_PSEUDO_TOOL_CALL_HEADER_RE = re.compile(
    r"\[Tool[\s_]call:\s*([A-Za-z_][A-Za-z0-9_]*)\s*\]",
    re.IGNORECASE,
)


def _scan_balanced_json_object(text: str, start: int) -> tuple[str, int] | None:
    """Find the next ``{...}`` JSON object starting at/after ``start``.

    Returns ``(json_str, end_index_exclusive)`` or ``None``. Tracks brace depth
    and respects string literals (handles escaped quotes), so nested objects
    inside the args don't trip the scanner.
    """
    n = len(text)
    i = start
    while i < n and text[i] != "{":
        if not text[i].isspace():
            return None
        i += 1
    if i >= n:
        return None

    depth = 0
    in_string = False
    escape = False
    j = i
    while j < n:
        ch = text[j]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[i : j + 1], j + 1
        j += 1
    return None


def extract_inline_tool_calls(text: str) -> tuple[list[dict[str, Any]], str]:
    """Extract tool calls from inline formats some models emit as text.

    Returns ``(calls, cleaned_text)``:

    - ``calls`` is a list of ``{"name", "arguments"}`` dicts (``arguments`` is
      a ``dict``).
    - ``cleaned_text`` is the original text with the matched pseudo blocks
      removed, so genuine narration around them survives without re-displaying
      the fake call.
    """
    results: list[dict[str, Any]] = []

    for m in _INVOKE_TAG_RE.finditer(text):
        tool_name = m.group(1).strip()
        body = m.group(2)
        args: dict[str, Any] = {}
        for pm in _PARAM_TAG_RE.finditer(body):
            args[pm.group(1).strip()] = pm.group(2).strip()
        results.append({"name": tool_name, "arguments": args})

    if not results:
        for m in _FUNC_TAG_RE.finditer(text):
            tool_name = m.group(1).strip()
            body = m.group(2)
            args = {}
            for pm in _PARAM_EQ_TAG_RE.finditer(body):
                args[pm.group(1).strip()] = pm.group(2).strip()
            results.append({"name": tool_name, "arguments": args})

    if results:
        return results, text

    cleaned_segments: list[str] = []
    cursor = 0
    for m in _PSEUDO_TOOL_CALL_HEADER_RE.finditer(text):
        tool_name = m.group(1).strip()
        scanned = _scan_balanced_json_object(text, m.end())
        if scanned is None:
            results.append({"name": tool_name, "arguments": {}})
            cleaned_segments.append(text[cursor : m.start()])
            cursor = m.end()
            continue
        json_str, end_idx = scanned
        try:
            args_obj = json.loads(json_str)
            if not isinstance(args_obj, dict):
                args_obj = {}
        except json.JSONDecodeError:
            args_obj = {}
        results.append({"name": tool_name, "arguments": args_obj})
        cleaned_segments.append(text[cursor : m.start()])
        cursor = end_idx

    if not results:
        return [], text

    cleaned_segments.append(text[cursor:])
    cleaned_text = "".join(cleaned_segments)
    cleaned_text = re.sub(r"\n{3,}", "\n\n", cleaned_text).strip()
    return results, cleaned_text


# ---------------------------------------------------------------------------
# Streaming-time stripping (display path)
# ---------------------------------------------------------------------------
#
# Ported from deeppath-api's production SSE filters
# (``_strip_pseudo_fn_chunk`` / ``_strip_pseudo_md_tool_call_chunk`` /
# ``_strip_pseudo_fn_final`` / ``_split_trailing_high_surrogate``). Recovery
# above decides what *executes*; this filter decides what the *user sees* —
# pseudo blocks and self-echoed fake tool results are swallowed before they
# leak into the display stream.

#: Echo-block openers a model may emit when it regresses into simulating both
#: sides of the tool conversation (Claude context overflow, HF/OpenAI
#: fine-tuning dataset convention). Both halves are stripped so the user
#: never sees fabricated "tool" output that never ran.
_PSEUDO_FN_OPEN_TAGS: tuple[str, ...] = (
    "<function_results>",
    "<function_calls>",
    "<tool_call>",
    "<tool_response>",
)
_PSEUDO_FN_CLOSE_TAGS: tuple[str, ...] = (
    "</function_results>",
    "</function_calls>",
    "</tool_call>",
    "</tool_response>",
)
_PSEUDO_FN_MAX_OPEN_LEN = max(len(t) for t in _PSEUDO_FN_OPEN_TAGS)
_PSEUDO_FN_MAX_CLOSE_LEN = max(len(t) for t in _PSEUDO_FN_CLOSE_TAGS)
#: Safety belt: never swallow forever — past this many swallowed chars, give
#: up and release the buffer (duplicate echo beats vanished output).
_PSEUDO_FN_SWALLOW_HARD_CAP = 32_000

_PSEUDO_MD_OPEN = "[Tool call:"
#: Release if no closing ``}`` arrives within this many swallowed chars.
_PSEUDO_MD_HARD_CAP = 4096

_PSEUDO_FN_FINAL_RE = re.compile(
    r"<(?:function_results|function_calls|tool_call|tool_response)>"
    r"[\s\S]*?"
    r"(?:</(?:function_results|function_calls|tool_call|tool_response)>|$)",
    re.IGNORECASE,
)


def split_trailing_high_surrogate(text: str, carry: str) -> tuple[str, str]:
    """Hold back a half-emoji split across stream chunk boundaries.

    Providers occasionally chop a 4-byte UTF-8 emoji mid-pair, handing us a
    string ending in a *high* UTF-16 surrogate (``U+D800–U+DBFF``) without
    its low half; encoding that with strict UTF-8 raises and tears down the
    stream. The fix: keep the trailing high surrogate in ``carry`` and
    prepend it to the next chunk, reconstructing the pair losslessly.
    """

    combined = (carry or "") + (text or "")
    if combined and 0xD800 <= ord(combined[-1]) <= 0xDBFF:
        return combined[:-1], combined[-1]
    return combined, ""


def strip_pseudo_fn_final(text: str) -> str:
    """Belt-and-suspenders cleanup for the joined assistant text.

    Catches echo blocks that slipped past the streaming filter (model never
    emitted a closing tag, or a non-streamed call produced the same noise).
    """

    if not text:
        return text
    return _PSEUDO_FN_FINAL_RE.sub("", text)


class _FnEchoFilter:
    """Stateful filter dropping ``<function_results>``-family echo blocks."""

    def __init__(self) -> None:
        self._swallow = False
        self._buf = ""
        self._swallowed = 0

    def _reset(self) -> None:
        self._swallow = False
        self._buf = ""
        self._swallowed = 0

    def feed(self, chunk: str) -> str:
        buf = self._buf + (chunk or "")
        out: list[str] = []
        while True:
            if self._swallow:
                close_idx, close_len = -1, 0
                for tag in _PSEUDO_FN_CLOSE_TAGS:
                    idx = buf.find(tag)
                    if idx >= 0 and (close_idx < 0 or idx < close_idx):
                        close_idx, close_len = idx, len(tag)
                if close_idx < 0:
                    tail_keep = _PSEUDO_FN_MAX_CLOSE_LEN - 1
                    if len(buf) > tail_keep:
                        self._swallowed += len(buf) - tail_keep
                        buf = buf[-tail_keep:]
                    if self._swallowed >= _PSEUDO_FN_SWALLOW_HARD_CAP:
                        out.append(buf)
                        self._reset()
                        return "".join(out)
                    self._buf = buf
                    return "".join(out)
                self._swallowed += close_idx
                buf = buf[close_idx + close_len :]
                self._swallow = False
                self._swallowed = 0
                continue

            open_idx, open_len = -1, 0
            for tag in _PSEUDO_FN_OPEN_TAGS:
                idx = buf.find(tag)
                if idx >= 0 and (open_idx < 0 or idx < open_idx):
                    open_idx, open_len = idx, len(tag)
            if open_idx >= 0:
                if open_idx > 0:
                    out.append(buf[:open_idx])
                buf = buf[open_idx + open_len :]
                self._swallow = True
                continue

            # No full opener: hold back a tail that might be a partial opener
            # straddling chunk boundaries (cheap fast path when no '<').
            if "<" not in buf:
                out.append(buf)
                self._reset()
                return "".join(out)
            tail_keep = _PSEUDO_FN_MAX_OPEN_LEN - 1
            if len(buf) > tail_keep:
                out.append(buf[:-tail_keep])
                buf = buf[-tail_keep:]
            self._buf = buf
            return "".join(out)

    def flush(self) -> str:
        # Still inside an unclosed block at stream end: drop the remainder —
        # a half-echo adds zero value.
        if self._swallow:
            self._reset()
            return ""
        tail = self._buf
        self._reset()
        return tail


class _MdCallFilter:
    """Stateful filter dropping ``[Tool call: name]\\n{json}`` blocks.

    Caveat (ported): assumes the JSON args are a single flat object — no
    nested ``}`` before the real closing brace. The hard cap protects the
    worst case.
    """

    def __init__(self) -> None:
        self._swallow = False
        self._buf = ""
        self._swallowed = 0

    def _reset(self) -> None:
        self._swallow = False
        self._buf = ""
        self._swallowed = 0

    def feed(self, chunk: str) -> str:
        buf = self._buf + (chunk or "")
        out: list[str] = []
        while True:
            if self._swallow:
                close_idx = buf.find("}")
                if close_idx < 0:
                    if len(buf) > 64:
                        self._swallowed += len(buf) - 64
                        buf = buf[-64:]
                    if self._swallowed >= _PSEUDO_MD_HARD_CAP:
                        out.append(buf)
                        self._reset()
                        return "".join(out)
                    self._buf = buf
                    return "".join(out)
                after = close_idx + 1
                if after < len(buf) and buf[after] == "\n":
                    after += 1
                buf = buf[after:]
                self._swallow = False
                self._swallowed = 0
                continue

            open_idx = buf.find(_PSEUDO_MD_OPEN)
            if open_idx >= 0:
                if open_idx > 0:
                    out.append(buf[:open_idx])
                buf = buf[open_idx + len(_PSEUDO_MD_OPEN) :]
                self._swallow = True
                continue

            if "[" not in buf:
                out.append(buf)
                self._reset()
                return "".join(out)
            keep = len(_PSEUDO_MD_OPEN) - 1
            if len(buf) > keep:
                out.append(buf[:-keep])
                buf = buf[-keep:]
            self._buf = buf
            return "".join(out)

    def flush(self) -> str:
        if self._swallow:
            self._reset()
            return ""
        tail = self._buf
        self._reset()
        return tail


class PseudoStreamStripper:
    """Display-path filter chaining the fn-echo and markdown-call filters.

    Usage: ``feed`` each raw content delta, emit what comes back; at stream
    end, ``flush`` once and emit the tail. Text held back to detect markers
    split across chunks is released on the next feed or at flush.
    """

    def __init__(self) -> None:
        self._fn = _FnEchoFilter()
        self._md = _MdCallFilter()

    def feed(self, chunk: str) -> str:
        return self._md.feed(self._fn.feed(chunk))

    def flush(self) -> str:
        return self._md.feed(self._fn.flush()) + self._md.flush()
