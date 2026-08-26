"""Parallel tool execution: consecutive concurrency-safe calls in one round
run under asyncio.gather; unsafe calls form barriers; event order stays
deterministic."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from steerable_agent_runtime import (
    CoreLoop,
    LoopConfig,
    LoopEvent,
    RouterToolExecutor,
    ToolRouter,
)
from steerable_agent_runtime.llm import LLMMessage

from test_trace_recorder import make_provider, tc


def _kinds_for(events: list[LoopEvent], *kinds: str) -> list[LoopEvent]:
    return [e for e in events if e.kind in kinds]


@pytest.mark.asyncio
async def test_safe_calls_run_concurrently() -> None:
    """Two read tools whose handlers rendezvous: each waits for the other to
    start — only possible if both run in the same gather."""
    router = ToolRouter()
    started: dict[str, asyncio.Event] = {"a": asyncio.Event(), "b": asyncio.Event()}

    async def read_a() -> str:
        started["a"].set()
        await asyncio.wait_for(started["b"].wait(), timeout=2)
        return "A"

    async def read_b() -> str:
        started["b"].set()
        await asyncio.wait_for(started["a"].wait(), timeout=2)
        return "B"

    router.register(read_a)
    router.register(read_b)
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("read_a"), tc("read_b")]},
            {"content": "both done"},
        ]
    )
    loop = CoreLoop(provider, RouterToolExecutor(router))

    events: list[LoopEvent] = []
    async for event in loop.run([LLMMessage(role="user", content="go")]):
        events.append(event)

    results = _kinds_for(events, "tool_call_result")
    assert len(results) == 2
    assert all(e.data["success"] for e in results)
    assert events[-1].data["status"] == "completed"


@pytest.mark.asyncio
async def test_result_events_follow_call_order_not_completion_order() -> None:
    """B finishes long before A, but events and transcript stay in call order."""
    router = ToolRouter()

    async def read_slow() -> str:
        await asyncio.sleep(0.05)
        return "slow"

    async def read_fast() -> str:
        return "fast"

    router.register(read_slow)
    router.register(read_fast)
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("read_slow"), tc("read_fast")]},
            {"content": "done"},
        ]
    )
    loop = CoreLoop(provider, RouterToolExecutor(router))

    events: list[LoopEvent] = []
    async for event in loop.run([LLMMessage(role="user", content="go")]):
        events.append(event)

    ordered = _kinds_for(events, "tool_call_start", "tool_call_result")
    names = [(e.kind, e.data["name"]) for e in ordered]
    assert names == [
        ("tool_call_start", "read_slow"),
        ("tool_call_start", "read_fast"),
        ("tool_call_result", "read_slow"),
        ("tool_call_result", "read_fast"),
    ]


@pytest.mark.asyncio
async def test_unsafe_call_forms_barrier() -> None:
    """safe, unsafe, safe → three sequential batches; the write tool never
    overlaps with either read."""
    router = ToolRouter()
    active = 0
    max_active = 0
    write_observed_active = -1

    async def read_x() -> str:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return "x"

    async def write_y() -> str:
        nonlocal active, write_observed_active
        write_observed_active = active
        await asyncio.sleep(0.01)
        return "y"

    router.register(read_x, name="read_x1")
    router.register(write_y)

    provider = make_provider(
        [
            {
                "content": "",
                "tool_calls": [
                    tc("read_x1"),
                    tc("write_y"),
                    tc("read_x1", {"q": 2}),
                ],
            },
            {"content": "done"},
        ]
    )
    loop = CoreLoop(provider, RouterToolExecutor(router))

    async for _ in loop.run([LLMMessage(role="user", content="go")]):
        pass

    # the write ran alone (no read active alongside it)
    assert write_observed_active == 0


@pytest.mark.asyncio
async def test_dedup_still_applies_inside_parallel_batch() -> None:
    router = ToolRouter()
    calls = 0

    async def read_a() -> str:
        nonlocal calls
        calls += 1
        return "A"

    router.register(read_a)
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("read_a"), tc("read_a")]},
            {"content": "done"},
        ]
    )
    loop = CoreLoop(provider, RouterToolExecutor(router))

    events: list[LoopEvent] = []
    async for event in loop.run([LLMMessage(role="user", content="go")]):
        events.append(event)

    assert calls == 1  # second identical call deduped, not executed
    results = _kinds_for(events, "tool_call_result")
    assert results[0].data["success"] is True
    assert results[1].data["success"] is False
    assert results[1].data["error"] == "duplicate_call"


@pytest.mark.asyncio
async def test_parallel_error_and_success_counters_in_order() -> None:
    """One failing + one succeeding safe call in a batch: counters applied in
    call order (failure then success resets the consecutive counter)."""
    router = ToolRouter()

    async def read_bad() -> str:
        raise RuntimeError("boom")

    async def read_good() -> str:
        return "ok"

    router.register(read_bad)
    router.register(read_good)
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("read_bad"), tc("read_good")]},
            {"content": "recovered"},
        ]
    )
    loop = CoreLoop(provider, RouterToolExecutor(router))

    events: list[LoopEvent] = []
    async for event in loop.run([LLMMessage(role="user", content="go")]):
        events.append(event)

    # RouterToolExecutor converts handler exceptions into failed ToolResults
    # (tool_error events are only for executor-level raises), so the failure
    # surfaces as an unsuccessful result carrying the error.
    results = _kinds_for(events, "tool_call_result")
    assert results[0].data["name"] == "read_bad"
    assert results[0].data["success"] is False
    assert "boom" in results[0].data["error"]
    assert results[1].data["name"] == "read_good"
    assert results[1].data["success"] is True
    stage = _kinds_for(events, "stage_complete")[-1]
    # bad (error → 1) then good (success → reset): ends at 0
    assert stage.data["consecutiveToolErrors"] == 0
    assert events[-1].data["status"] == "completed"


@pytest.mark.asyncio
async def test_executor_without_safety_check_stays_serial() -> None:
    """Executors not implementing concurrency_safe (e.g. host reverse
    channel) run every call sequentially even with parallel_tools on."""
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("x"), tc("y")]},
            {"content": "done"},
        ]
    )
    overlap = 0
    active = 0

    class SerialExecutor:
        async def execute(self, call, ctx):
            nonlocal active, overlap
            active += 1
            overlap = max(overlap, active)
            await asyncio.sleep(0.01)
            active -= 1
            from steerable_agent_protocol.generated import ToolResult

            return ToolResult(success=True, data={"value": call.name})

    loop = CoreLoop(provider, SerialExecutor())  # no concurrency_safe method
    async for _ in loop.run([LLMMessage(role="user", content="go")]):
        pass

    assert overlap == 1


@pytest.mark.asyncio
async def test_parallel_tools_disabled_by_config() -> None:
    router = ToolRouter()
    overlap = 0
    active = 0

    async def read_a() -> str:
        nonlocal active, overlap
        active += 1
        overlap = max(overlap, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "A"

    router.register(read_a)
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("read_a"), tc("read_a", {"q": 1})]},
            {"content": "done"},
        ]
    )
    loop = CoreLoop(
        provider, RouterToolExecutor(router), LoopConfig(parallel_tools=False)
    )
    async for _ in loop.run([LLMMessage(role="user", content="go")]):
        pass

    assert overlap == 1
