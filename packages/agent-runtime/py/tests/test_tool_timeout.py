"""Per-tool timeout: a hung tool returns a failed ToolResult instead of
hanging the turn, and the consecutive-error breaker still handles it.

``soft_timeout_ms`` is only checked at round boundaries, so before this the
turn had no defense against a tool that never returns — the case a remote
executor (reverse channel, MCP) *will* eventually hit.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime import (
    CoreLoop,
    LoopConfig,
    LoopContext,
    LoopEvent,
    RouterToolExecutor,
    ToolRouter,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk


def make_provider(script: list[dict[str, Any]]):
    class _FakeProvider:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            entry = script[min(self._idx, len(script) - 1)]
            self._idx += 1

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                content = entry.get("content", "")
                if content:
                    yield LLMStreamChunk(content_delta=content)
                for call in entry.get("tool_calls", []):
                    yield LLMStreamChunk(tool_call_delta=call)
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop"
                )

            return _gen()

    return _FakeProvider()


def tc(name: str, args: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(id=f"call_{name}", name=name, arguments=args or {})


async def collect(loop_run: AsyncIterator[LoopEvent]) -> list[LoopEvent]:
    return [e async for e in loop_run]


def results_of(events: list[LoopEvent]) -> list[LoopEvent]:
    return [e for e in events if e.kind == "tool_call_result"]


@pytest.mark.asyncio
async def test_hung_tool_returns_failed_result_and_turn_completes() -> None:
    provider = make_provider(
        [{"content": "", "tool_calls": [tc("hang")]}, {"content": "recovered"}]
    )
    router = ToolRouter()

    async def hang() -> str:
        await asyncio.sleep(60)
        return "never"  # pragma: no cover — cancelled long before

    router.register(hang)
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(tool_timeout_ms=20),
    )
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    assert events[-1].data["status"] == "completed"
    results = results_of(events)
    assert len(results) == 1
    assert results[0].data["success"] is False
    assert results[0].data["error"] == "tool_timeout"
    # The transcript got a tool message (providers reject dangling calls) and
    # the model answered from it in round 2.
    assert events[-1].data["textLength"] == len("recovered")


@pytest.mark.asyncio
async def test_timeout_feeds_the_consecutive_error_breaker() -> None:
    # max_tool_errors=1: a single hung tool must trip the same breaker any
    # other tool failure trips.
    provider = make_provider([{"content": "", "tool_calls": [tc("hang")]}])
    router = ToolRouter()

    async def hang() -> str:
        await asyncio.sleep(60)
        return "never"  # pragma: no cover

    router.register(hang)
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(tool_timeout_ms=20, max_tool_errors=1),
    )
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    assert events[-1].data["status"] == "failed"
    assert events[-1].data["reason"] == "too many consecutive tool errors"


@pytest.mark.asyncio
async def test_timeout_applies_to_any_executor_including_remote() -> None:
    # The timeout wraps the executor port itself, not the ToolRouter — a
    # remote executor (reverse channel today, MCP later) gets the same
    # guarantee. Stand in for one with a bare executor that never returns.
    class _HungRemoteExecutor:
        async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
            await asyncio.sleep(60)
            return ToolResult(success=True)  # pragma: no cover

    provider = make_provider(
        [{"content": "", "tool_calls": [tc("remote_tool")]}, {"content": "answered"}]
    )
    loop = CoreLoop(
        provider,
        _HungRemoteExecutor(),
        LoopConfig(tool_timeout_ms=20),
    )
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    assert events[-1].data["status"] == "completed"
    results = results_of(events)
    assert len(results) == 1
    assert results[0].data["error"] == "tool_timeout"


@pytest.mark.asyncio
async def test_parallel_batch_isolates_the_hung_call() -> None:
    # Two concurrency-safe calls in one batch: the fast one succeeds, the
    # hung one times out, and the batch still completes in call order.
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("fast"), tc("hang")]},
            {"content": "done"},
        ]
    )
    router = ToolRouter()

    async def fast() -> str:
        return "quick"

    async def hang() -> str:
        await asyncio.sleep(60)
        return "never"  # pragma: no cover

    router.register(fast, mode="read")
    router.register(hang, mode="read")
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(tool_timeout_ms=20, parallel_tools=True),
    )
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    assert events[-1].data["status"] == "completed"
    results = results_of(events)
    assert [(r.data["name"], r.data["success"]) for r in results] == [
        ("fast", True),
        ("hang", False),
    ]
    assert results[1].data["error"] == "tool_timeout"


@pytest.mark.asyncio
async def test_none_disables_the_timeout() -> None:
    provider = make_provider(
        [{"content": "", "tool_calls": [tc("slow")]}, {"content": "done"}]
    )
    router = ToolRouter()
    finished: list[str] = []

    async def slow() -> str:
        await asyncio.sleep(0.05)
        finished.append("slow")
        return "ok"

    router.register(slow)
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(tool_timeout_ms=None),
    )
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))
    assert finished == ["slow"]
    assert results_of(events)[0].data["success"] is True


@pytest.mark.asyncio
async def test_slow_tool_under_the_limit_is_untouched() -> None:
    provider = make_provider(
        [{"content": "", "tool_calls": [tc("slowish")]}, {"content": "done"}]
    )
    router = ToolRouter()

    async def slowish() -> str:
        await asyncio.sleep(0.01)
        return "ok"

    router.register(slowish)
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(tool_timeout_ms=5_000),
    )
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))
    assert results_of(events)[0].data["success"] is True


def test_default_is_a_generous_backstop() -> None:
    assert LoopConfig().tool_timeout_ms == 300_000


def test_non_positive_timeout_rejected() -> None:
    with pytest.raises(ValueError, match="tool_timeout_ms"):
        LoopConfig(tool_timeout_ms=0)
    with pytest.raises(ValueError, match="tool_timeout_ms"):
        LoopConfig(tool_timeout_ms=-1)
    with pytest.raises(ValueError, match="wrap_up_tool_timeout_ms"):
        LoopConfig(wrap_up_tool_timeout_ms=0)
    with pytest.raises(ValueError, match="wrap_up_hard_cap_ms"):
        LoopConfig(wrap_up_hard_cap_ms=-1)


@pytest.mark.asyncio
async def test_wrap_up_hard_cap_caps_tool_before_wrap() -> None:
    provider = make_provider(
        [{"content": "", "tool_calls": [tc("hang")]}, {"content": "recovered"}]
    )
    router = ToolRouter()

    async def hang() -> str:
        await asyncio.sleep(60)
        return "never"  # pragma: no cover

    router.register(hang)
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(tool_timeout_ms=60_000, wrap_up_hard_cap_ms=30),
    )
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))
    assert events[-1].data["status"] == "completed"
    results = results_of(events)
    assert results[0].data["error"] == "tool_timeout"


@pytest.mark.asyncio
async def test_wrap_up_tool_timeout_caps_hung_write() -> None:
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("slow")]},
            {"content": "", "tool_calls": [tc("hang")]},
            {"content": "wrote what I could"},
        ]
    )
    router = ToolRouter()

    async def slow() -> str:
        await asyncio.sleep(0.05)
        return "slept"

    async def hang() -> str:
        await asyncio.sleep(60)
        return "never"  # pragma: no cover

    router.register(slow)
    router.register(hang)
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(
            soft_timeout_ms=10,
            wrap_up_keeps_tools=True,
            wrap_up_max_tool_rounds=2,
            wrap_up_tool_timeout_ms=20,
            tool_timeout_ms=60_000,
        ),
    )
    schemas = [
        {"type": "function", "function": {"name": "slow", "parameters": {}}},
        {"type": "function", "function": {"name": "hang", "parameters": {}}},
    ]
    events = await collect(
        loop.run([LLMMessage.text_of("user", "go")], tools=schemas)
    )
    assert events[-1].data["status"] == "completed"
    errors = [e.data.get("error") for e in results_of(events)]
    assert "tool_timeout" in errors
