"""A3 acceptance: replay a CoreLoop run's trajectory and check the reduced
execution state matches the loop's actual terminal decision.

This is the Python-side analogue of the agent's A2 replay contract: the loop
records one `step_decision` per round, and `reduce_execution_state` must fold
them back to the same terminal status the loop emitted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime import (
    CoreLoop,
    HarnessExecutionState,
    LoopConfig,
    LoopEvent,
    RouterToolExecutor,
    ToolRouter,
    reduce_execution_state,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk


def make_provider(script: list[dict[str, Any]]):
    class _FakeProvider:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self._idx = 0

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            entry = script[min(self._idx, len(script) - 1)]
            self._idx += 1

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                if entry.get("content"):
                    yield LLMStreamChunk(content_delta=entry["content"])
                for tc in entry.get("tool_calls", []):
                    yield LLMStreamChunk(tool_call_delta=tc)
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop"
                )

            return _gen()

    return _FakeProvider()


def tc(name: str) -> ToolCall:
    return ToolCall(id=f"call_{name}", name=name, arguments={})


async def run_and_replay(loop: CoreLoop) -> tuple[list[LoopEvent], HarnessExecutionState]:
    events = [e async for e in loop.run([LLMMessage.text_of("user", "go")])]
    # Round-trip the trajectory through JSON like a persisted payload would.
    import json

    raw = json.loads(json.dumps([e.to_dict() for e in loop.trajectory]))
    state = reduce_execution_state(HarnessExecutionState.new(), raw)
    return events, state


@pytest.mark.asyncio
async def test_completed_run_replays_to_completed() -> None:
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("add")]},
            {"content": "done"},
        ]
    )
    router = ToolRouter()

    async def add() -> int:
        return 1

    router.register(add)
    loop = CoreLoop(provider, RouterToolExecutor(router))

    events, state = await run_and_replay(loop)
    terminal = [e for e in events if e.kind == "completion"][-1]
    assert terminal.data["status"] == "completed"
    assert state.status == "completed"
    assert state.budgets.step_count == 2
    assert [s["traceStepId"] for s in state.steps] == ["round_0", "round_1"]


@pytest.mark.asyncio
async def test_failed_run_replays_to_failed() -> None:
    provider = make_provider([{"content": "", "tool_calls": [tc("boom")]}])
    router = ToolRouter()

    async def boom() -> None:
        raise RuntimeError("x")

    router.register(boom)
    loop = CoreLoop(provider, RouterToolExecutor(router), LoopConfig(max_tool_errors=2))

    events, state = await run_and_replay(loop)
    terminal = [e for e in events if e.kind == "completion"][-1]
    assert terminal.data["status"] == "failed"
    assert state.status == "failed"


@pytest.mark.asyncio
async def test_runaway_replays_to_budget_exhausted() -> None:
    provider = make_provider([{"content": "", "tool_calls": [tc("ping")]}])
    router = ToolRouter()

    async def ping() -> str:
        return "pong"

    router.register(ping)
    loop = CoreLoop(provider, RouterToolExecutor(router), LoopConfig(max_rounds=3))

    events, state = await run_and_replay(loop)
    terminal = [e for e in events if e.kind == "completion"][-1]
    assert terminal.data["status"] == "budget_exhausted"
    assert state.status == "budget_exhausted"


@pytest.mark.asyncio
async def test_trajectory_records_tool_names_per_step() -> None:
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("a"), tc("b")]},
            {"content": "ok"},
        ]
    )
    router = ToolRouter()
    router.register(lambda: "x", name="a")
    router.register(lambda: "y", name="b")
    loop = CoreLoop(provider, RouterToolExecutor(router))

    _events, state = await run_and_replay(loop)
    first_step = state.steps[0]
    assert first_step["toolCalls"] == ["a", "b"]
    assert first_step["toolCallCount"] == 2
