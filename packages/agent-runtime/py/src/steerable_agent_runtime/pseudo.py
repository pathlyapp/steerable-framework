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
collected. Streaming-time stripping for display cleanliness is a separate
concern (a later slice).
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
