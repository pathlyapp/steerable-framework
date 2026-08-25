"""Soft timeout: wall-clock budget → graceful wrap-up instead of hard kill."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime import (
    CoreLoop,
    LoopConfig,
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
            self.calls: list[list[LLMMessage]] = []
            self.tools_seen: list[Any] = []
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            self.calls.append(list(messages))
            self.tools_seen.append(tools)
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


@pytest.mark.asyncio
async def test_no_soft_timeout_runs_normally() -> None:
    provider = make_provider([{"content": "answer"}])
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()))
    events = await collect(loop.run([LLMMessage(role="user", content="hi")]))
    assert not [e for e in events if e.kind == "soft_timeout"]
    assert events[-1].data["status"] == "completed"


@pytest.mark.asyncio
async def test_soft_timeout_triggers_wrap_up_round() -> None:
    # Round 0 runs a (slow) tool; by round 1 the deadline has passed, so the
    # loop must emit soft_timeout, withhold tool descriptors, and the model's
    # final text completes the turn.
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("slow")]},
            {"content": "partial answer"},
        ]
    )
    router = ToolRouter()

    async def slow() -> str:
        await asyncio.sleep(0.05)
        return "slept"

    router.register(slow)
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(soft_timeout_ms=10),  # 10ms — round 0's tool blows past it
    )
    events = await collect(loop.run([LLMMessage(role="user", content="go")]))

    soft = [e for e in events if e.kind == "soft_timeout"]
    assert len(soft) == 1
    assert soft[0].data["round"] == 1
    # wrap-up round saw no tool descriptors
    assert provider.tools_seen[1] is None
    # the wrap-up notice was appended to the transcript
    assert any(
        "time budget" in (m.content or "") for m in provider.calls[1]
    )
    assert events[-1].data["status"] == "completed"


@pytest.mark.asyncio
async def test_wrap_up_drops_tool_intent() -> None:
    # Even if the model insists on calling tools in the wrap-up round, the
    # loop must not execute them — it completes with whatever content exists.
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("noop")]},
            # wrap-up round: model ignores the notice and emits a tool call
            # plus some final text
            {"content": "fine, wrapping up", "tool_calls": [tc("noop")]},
        ]
    )
    router = ToolRouter()
    executed: list[str] = []

    async def noop() -> str:
        executed.append("noop")
        await asyncio.sleep(0.02)  # push elapsed past the 10ms deadline
        return "ok"

    router.register(noop)
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(soft_timeout_ms=10),  # round 0 runs; deadline passes by round 1
    )
    events = await collect(loop.run([LLMMessage(role="user", content="go")]))

    # tool ran exactly once (round 0), not in the wrap-up round
    assert executed == ["noop"]
    assert events[-1].data["status"] == "completed"
    assert events[-1].data["textLength"] == len("fine, wrapping up")


@pytest.mark.asyncio
async def test_soft_timeout_never_interrupts_in_flight_tool() -> None:
    # Soft = checked only at round boundaries. A tool slower than the whole
    # budget still runs to completion.
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
        LoopConfig(soft_timeout_ms=1),
    )
    await collect(loop.run([LLMMessage(role="user", content="go")]))
    assert finished == ["slow"]
