"""Large tool-result externalization (spill).

Local shell / file tools can return megabytes of output. Serialized straight
into the transcript (``json.dumps`` in the loop) that output blows the small
local context window in a single round. This module is the ``post_tool_result``
hook consumer that prevents it — modeled on dsh's ``spill-policy``:

1. Let the tool run (the hook fires *after* execution).
2. Serialize the result payload; if its UTF-8 size is ``<= max_inline_bytes``,
   pass it through unchanged.
3. Otherwise save the full text to a ``SpillStore`` and replace the payload
   with a head/tail preview plus a locator the model can use to read more.

The model-facing contract: the rewritten ``data`` carries ``spilled: true``,
``locator``, ``total_bytes``, and ``preview`` so the model knows the output
was truncated and where the full content lives.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from steerable_agent_protocol.generated import ToolCall, ToolResult

from .hooks import NoopHooks

# ---------------------------------------------------------------------------
# SpillStore
# ---------------------------------------------------------------------------


@runtime_checkable
class SpillStore(Protocol):
    """Where spilled payloads go. Returns a locator string the model sees."""

    def save(self, content: str, *, tool: str) -> str: ...


class FilesystemSpillStore:
    """Spill to a directory on disk; locator is the absolute file path."""

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def save(self, content: str, *, tool: str) -> str:
        safe_tool = re.sub(r"[^A-Za-z0-9_.-]+", "_", tool) or "tool"
        path = self._dir / f"{safe_tool}-{uuid.uuid4().hex[:12]}.txt"
        path.write_text(content, encoding="utf-8")
        return str(path)


class InMemorySpillStore:
    """Test / ephemeral store; locator is an opaque key."""

    def __init__(self) -> None:
        self._items: dict[str, str] = {}

    def save(self, content: str, *, tool: str) -> str:
        key = f"mem://{tool}/{uuid.uuid4().hex[:12]}"
        self._items[key] = content
        return key

    def get(self, locator: str) -> str | None:
        return self._items.get(locator)


# ---------------------------------------------------------------------------
# SpillHooks
# ---------------------------------------------------------------------------


class SpillHooks(NoopHooks):
    """``post_tool_result`` hook: spill oversized result payloads.

    Only the ``data`` payload is considered for spilling (``error`` /
    ``message`` are short by construction). Non-text payloads are spilled via
    their JSON serialization.
    """

    def __init__(
        self,
        store: SpillStore,
        *,
        max_inline_bytes: int = 16_000,
        preview_bytes: int = 2_000,
    ) -> None:
        self._store = store
        self._max_inline = max_inline_bytes
        self._preview = preview_bytes

    async def post_tool_result(
        self, result: ToolResult, call: ToolCall, ctx: Any
    ) -> ToolResult:
        if result.data is None:
            return result
        serialized = json.dumps(result.data, ensure_ascii=False)
        total_bytes = len(serialized.encode("utf-8"))
        if total_bytes <= self._max_inline:
            return result

        locator = self._store.save(serialized, tool=call.name)
        preview = _head_tail(serialized, self._preview)
        result.data = {
            "spilled": True,
            "locator": locator,
            "total_bytes": total_bytes,
            "preview": preview,
            "note": (
                "Output exceeded the inline budget and was saved externally. "
                "Read `locator` for the full content."
            ),
        }
        return result


def _head_tail(text: str, budget: int) -> str:
    """Keep the head and tail of ``text`` within ``budget`` chars."""
    if len(text) <= budget:
        return text
    half = max(budget // 2, 1)
    omitted = len(text) - 2 * half
    return f"{text[:half]}\n…[{omitted} chars omitted]…\n{text[-half:]}"
