"""SpillHooks: large tool-result externalization via the post_tool_result hook."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime import (
    CoreLoop,
    InMemorySpillStore,
    LoopEvent,
    RouterToolExecutor,
    SpillHooks,
    ToolRouter,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk


def make_provider(script: list[dict[str, Any]]):
    class _FakeProvider:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self.calls: list[list[LLMMessage]] = []
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            self.calls.append(list(messages))
            entry = script[min(self._idx, len(script) - 1)]
            self._idx += 1

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                content = entry.get("content", "")
                if content:
                    yield LLMStreamChunk(content_delta=content)
                for tc in entry.get("tool_calls", []):
                    yield LLMStreamChunk(tool_call_delta=tc)
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop",
                    usage=entry.get("usage"),
                )

            return _gen()

    return _FakeProvider()


def tc(name: str, args: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(id=f"call_{name}", name=name, arguments=args or {})


async def collect(loop_run: AsyncIterator[LoopEvent]) -> list[LoopEvent]:
    return [e async for e in loop_run]


@pytest.mark.asyncio
async def test_small_result_passes_through_unchanged() -> None:
    provider = make_provider(
        [{"content": "", "tool_calls": [tc("add", {"a": 1, "b": 2})]}, {"content": "done"}]
    )
    router = ToolRouter()

    async def add(a: int, b: int) -> int:
        return a + b

    router.register(add)
    store = InMemorySpillStore()
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        hooks=SpillHooks(store, max_inline_bytes=16_000),
    )
    events = await collect(loop.run([LLMMessage.text_of("user", "add")]))

    # nothing spilled; the transcript carries the raw result
    assert store._items == {}
    tool_msgs = [m for m in provider.calls[1] if m.role == "tool"]
    assert '"value": 3' in tool_msgs[0].content_text
    assert any(e.kind == "tool_call_result" for e in events)


@pytest.mark.asyncio
async def test_large_result_is_spilled_with_preview_and_locator() -> None:
    big_output = "x" * 40_000
    provider = make_provider(
        [{"content": "", "tool_calls": [tc("shell")]}, {"content": "done"}]
    )
    router = ToolRouter()

    async def shell() -> str:
        return big_output

    router.register(shell)
    store = InMemorySpillStore()
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        hooks=SpillHooks(store, max_inline_bytes=1_000, preview_bytes=200),
    )
    await collect(loop.run([LLMMessage.text_of("user", "run")]))

    # full content landed in the store
    assert len(store._items) == 1
    locator, full = next(iter(store._items.items()))
    assert big_output in full

    # the transcript carries preview + locator, not the 40k blob
    tool_msgs = [m for m in provider.calls[1] if m.role == "tool"]
    assert len(tool_msgs) == 1
    body = tool_msgs[0].content_text
    assert '"spilled": true' in body
    assert locator in body
    assert "chars omitted" in body
    assert len(body) < 5_000


@pytest.mark.asyncio
async def test_spill_preserves_failure_results() -> None:
    provider = make_provider(
        [{"content": "", "tool_calls": [tc("boom")]}, {"content": "ok"}]
    )
    router = ToolRouter()

    async def boom() -> None:
        raise RuntimeError("nope")

    router.register(boom)
    store = InMemorySpillStore()
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        hooks=SpillHooks(store, max_inline_bytes=1_000),
    )
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    # failure results pass through (error field is short; data may be absent)
    failed = [e for e in events if e.kind == "tool_call_result" and not e.data["success"]]
    assert len(failed) == 1
