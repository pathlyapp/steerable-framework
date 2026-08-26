"""Mid-turn steering: CoreLoop.steer injects a user message that the loop
consumes at the next round boundary."""

from __future__ import annotations

from typing import Any

import pytest
from steerable_agent_runtime import (
    CoreLoop,
    LoopEvent,
    RouterToolExecutor,
    ToolRouter,
)
from steerable_agent_runtime.llm import LLMMessage

from test_trace_recorder import make_provider, tc


@pytest.mark.asyncio
async def test_steer_consumed_at_next_round() -> None:
    """A message steered during tool execution appears in the transcript the
    provider sees on the following round."""
    router = ToolRouter()
    loop_holder: dict[str, CoreLoop] = {}

    async def get_data() -> str:
        loop_holder["loop"].steer("补充：只要最近一周的数据")
        return "rows"

    router.register(get_data)
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("get_data")]},
            {"content": "done"},
        ]
    )
    seen: list[list[LLMMessage]] = []
    original_stream = provider.stream

    def capturing_stream(messages, **kw):
        seen.append(list(messages))
        return original_stream(messages, **kw)

    provider.stream = capturing_stream  # type: ignore[method-assign]

    loop = CoreLoop(provider, RouterToolExecutor(router))
    loop_holder["loop"] = loop
    events: list[LoopEvent] = []
    async for event in loop.run([LLMMessage(role="user", content="查数据")]):
        events.append(event)

    # steer event surfaced
    steer_events = [e for e in events if e.kind == "steer"]
    assert len(steer_events) == 1
    assert steer_events[0].data["content"] == "补充：只要最近一周的数据"

    # second LLM round saw the injected user message at the end
    assert len(seen) == 2
    assert seen[1][-1].role == "user"
    assert seen[1][-1].content == "补充：只要最近一周的数据"

    assert events[-1].data["status"] == "completed"


@pytest.mark.asyncio
async def test_multiple_steers_append_in_order() -> None:
    router = ToolRouter()
    loop_holder: dict[str, CoreLoop] = {}

    async def get_data() -> str:
        loop_holder["loop"].steer("第一条补充")
        loop_holder["loop"].steer("第二条补充")
        return "rows"

    router.register(get_data)
    provider = make_provider(
        [{"content": "", "tool_calls": [tc("get_data")]}, {"content": "done"}]
    )
    seen: list[list[LLMMessage]] = []
    original_stream = provider.stream

    def capturing_stream(messages, **kw):
        seen.append(list(messages))
        return original_stream(messages, **kw)

    provider.stream = capturing_stream  # type: ignore[method-assign]

    loop = CoreLoop(provider, RouterToolExecutor(router))
    loop_holder["loop"] = loop
    async for _ in loop.run([LLMMessage(role="user", content="查数据")]):
        pass

    tail = [m.content for m in seen[1] if m.role == "user"]
    assert tail[-2:] == ["第一条补充", "第二条补充"]


@pytest.mark.asyncio
async def test_steer_before_run_lands_in_first_round() -> None:
    """A message steered before the first round is part of the initial
    transcript the provider sees."""
    provider = make_provider([{"content": "ok"}])
    seen: list[list[LLMMessage]] = []
    original_stream = provider.stream

    def capturing_stream(messages, **kw):
        seen.append(list(messages))
        return original_stream(messages, **kw)

    provider.stream = capturing_stream  # type: ignore[method-assign]

    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()))
    loop.steer("提前补充")
    async for _ in loop.run([LLMMessage(role="user", content="hi")]):
        pass

    assert seen[0][-1].content == "提前补充"


@pytest.mark.asyncio
async def test_steer_after_run_is_harmless() -> None:
    provider = make_provider([{"content": "ok"}])
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()))
    async for _ in loop.run([LLMMessage(role="user", content="hi")]):
        pass
    loop.steer("太晚了")  # no consumer — must not raise
    loop.steer("")  # empty content ignored
