"""Mid-turn steering: CoreLoop.steer injects a user message that the loop
consumes at the next round boundary."""

from __future__ import annotations

from typing import Any

import pytest
from steerable_agent_runtime import (
    LoopConfig,
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
    async for event in loop.run([LLMMessage.text_of("user", "查数据")]):
        events.append(event)

    # steer event surfaced
    steer_events = [e for e in events if e.kind == "steer"]
    assert len(steer_events) == 1
    assert steer_events[0].data["content"] == "补充：只要最近一周的数据"

    # second LLM round saw the injected user message at the end
    assert len(seen) == 2
    assert seen[1][-1].role == "user"
    assert seen[1][-1].content_text == "补充：只要最近一周的数据"

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
    async for _ in loop.run([LLMMessage.text_of("user", "查数据")]):
        pass

    tail = [m.content_text for m in seen[1] if m.role == "user"]
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
    async for _ in loop.run([LLMMessage.text_of("user", "hi")]):
        pass

    assert seen[0][-1].content_text == "提前补充"


@pytest.mark.asyncio
async def test_steer_after_run_is_harmless() -> None:
    provider = make_provider([{"content": "ok"}])
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()))
    async for _ in loop.run([LLMMessage.text_of("user", "hi")]):
        pass
    loop.steer("太晚了")  # no consumer — must not raise
    loop.steer("")  # empty content ignored


# ─── W2.8.1: steer_mode="interrupt" ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_interrupt_mode_cancels_inflight_tool_and_continues() -> None:
    """A steer arriving mid-tool cancels the in-flight call; the turn
    continues and the model sees the steer at the very next request."""
    import asyncio

    router = ToolRouter()
    loop_holder: dict[str, CoreLoop] = {}

    async def slow_tool() -> str:
        loop_holder["loop"].steer("别查了，直接总结")
        await asyncio.sleep(60)  # long enough that only the interrupt ends it
        return "rows"

    router.register(slow_tool)
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("slow_tool")]},
            {"content": "总结：已停止查询"},
        ]
    )
    seen: list[list[LLMMessage]] = []
    original_stream = provider.stream

    def capturing_stream(messages, **kw):
        seen.append(list(messages))
        return original_stream(messages, **kw)

    provider.stream = capturing_stream  # type: ignore[method-assign]

    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        config=LoopConfig(steer_mode="interrupt"),
    )
    loop_holder["loop"] = loop
    events: list[LoopEvent] = []
    async for event in loop.run([LLMMessage.text_of("user", "查数据")]):
        events.append(event)

    # The tool was interrupted, not completed, and not counted as a failure
    # that ends the turn — the run completes normally on the next round.
    tool_errors = [e for e in events if e.kind == "tool_error"]
    assert len(tool_errors) == 1
    assert "interrupted" in tool_errors[0].data["error"]
    assert events[-1].data["status"] == "completed"

    # The very next request carries the interrupted call's synthetic result
    # AND the steered message — no dangling tool_calls, no extra round.
    assert len(seen) == 2
    second = seen[1]
    tool_msgs = [m for m in second if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "interrupted" in tool_msgs[0].content_text
    assert second[-1].role == "user"
    assert second[-1].content_text == "别查了，直接总结"


@pytest.mark.asyncio
async def test_interrupt_mode_skips_unstarted_calls() -> None:
    """Calls after the interrupted one get skip notices (no dangling
    tool_calls) and never execute."""
    import asyncio

    router = ToolRouter()
    loop_holder: dict[str, CoreLoop] = {}
    ran: list[str] = []

    async def first() -> str:
        loop_holder["loop"].steer("停")
        await asyncio.sleep(60)
        ran.append("first")
        return "1"

    async def second() -> str:
        ran.append("second")
        return "2"

    router.register(first)
    router.register(second)
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("first"), tc("second")]},
            {"content": "done"},
        ]
    )
    seen: list[list[LLMMessage]] = []
    original_stream = provider.stream

    def capturing_stream(messages, **kw):
        seen.append(list(messages))
        return original_stream(messages, **kw)

    provider.stream = capturing_stream  # type: ignore[method-assign]

    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        # Serial execution: RouterToolExecutor without concurrency_safe
        # declarations keeps calls sequential.
        config=LoopConfig(steer_mode="interrupt", parallel_tools=False),
    )
    loop_holder["loop"] = loop
    events: list[LoopEvent] = []
    async for event in loop.run([LLMMessage.text_of("user", "go")]):
        events.append(event)

    assert ran == []  # neither tool body completed
    assert events[-1].data["status"] == "completed"
    # Both calls have tool messages in the second request's transcript.
    assert len(seen) == 2
    tool_msgs = [m for m in seen[1] if m.role == "tool"]
    assert len(tool_msgs) == 2
    assert any("not executed" in m.content_text for m in tool_msgs)


@pytest.mark.asyncio
async def test_boundary_mode_unchanged_default() -> None:
    """Default config: a mid-tool steer waits for the round boundary — the
    tool runs to completion (existing behavior preserved)."""
    router = ToolRouter()
    loop_holder: dict[str, CoreLoop] = {}

    async def quick_tool() -> str:
        loop_holder["loop"].steer("补充")
        return "rows"

    router.register(quick_tool)
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("quick_tool")]},
            {"content": "done"},
        ]
    )
    loop = CoreLoop(provider, RouterToolExecutor(router))  # default boundary
    loop_holder["loop"] = loop
    events: list[LoopEvent] = []
    async for event in loop.run([LLMMessage.text_of("user", "hi")]):
        events.append(event)

    assert not [e for e in events if e.kind == "tool_error"]
    assert events[-1].data["status"] == "completed"


@pytest.mark.asyncio
async def test_invalid_steer_mode_rejected() -> None:
    with pytest.raises(ValueError, match="steer_mode"):
        LoopConfig(steer_mode="yolo")  # type: ignore[arg-type]
