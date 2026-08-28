"""CoreLoop minimal-slice tests: mock LLM + tools, drive a turn to completion."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_harness import BudgetLimit
from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime import (
    CoreLoop,
    LoopConfig,
    LoopEvent,
    RouterToolExecutor,
    ToolRouter,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk, LLMUsage


def make_provider(script: list[dict[str, Any]]):
    """Build a fake LLMProvider that plays back a scripted sequence of turns.

    Each script entry: {"content": str, "tool_calls": [ToolCall], "usage": LLMUsage}
    The provider pops one entry per stream() call; extra calls replay the last.
    """

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
                usage = entry.get("usage")
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop",
                    usage=usage,
                )

            return _gen()

    return _FakeProvider()


def tc(name: str, args: dict[str, Any] | None = None, call_id: str | None = None) -> ToolCall:
    return ToolCall(id=call_id or f"call_{name}", name=name, arguments=args or {})


async def collect(loop_run: AsyncIterator[LoopEvent]) -> list[LoopEvent]:
    return [e async for e in loop_run]


def final_completion(events: list[LoopEvent]) -> dict[str, Any]:
    completions = [e for e in events if e.kind == "completion"]
    assert completions, "loop never emitted a completion event"
    return completions[-1].data


@pytest.mark.asyncio
async def test_no_tool_calls_completes() -> None:
    provider = make_provider([{"content": "The answer is 4."}])
    router = ToolRouter()
    loop = CoreLoop(provider, RouterToolExecutor(router))

    events = await collect(loop.run([LLMMessage.text_of("user", "2+2?")]))
    decision = final_completion(events)
    assert decision["status"] == "completed"
    # content streamed through
    deltas = [e.data["delta"] for e in events if e.kind == "content_delta"]
    assert "".join(deltas) == "The answer is 4."


@pytest.mark.asyncio
async def test_tool_round_then_completion() -> None:
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("add", {"a": 1, "b": 2})]},
            {"content": "Sum is 3."},
        ]
    )
    router = ToolRouter()

    async def add(a: int, b: int) -> int:
        return a + b

    router.register(add)
    loop = CoreLoop(provider, RouterToolExecutor(router))

    events = await collect(loop.run([LLMMessage.text_of("user", "add")]))
    decision = final_completion(events)
    assert decision["status"] == "completed"

    # tool executed and result fed back into the transcript
    starts = [e for e in events if e.kind == "tool_call_start"]
    results = [e for e in events if e.kind == "tool_call_result"]
    assert len(starts) == 1 and starts[0].data["name"] == "add"
    assert len(results) == 1 and results[0].data["success"] is True

    # second LLM call saw the tool observation message
    second_call_messages = provider.calls[1]
    tool_msgs = [m for m in second_call_messages if m.role == "tool"]
    assert len(tool_msgs) == 1 and tool_msgs[0].name == "add"
    assert '"success": true' in tool_msgs[0].content_text


@pytest.mark.asyncio
async def test_consecutive_tool_errors_trip_breaker() -> None:
    # Every turn the model calls a failing tool; after max_tool_errors consecutive
    # failures the loop must stop with failed.
    provider = make_provider([{"content": "", "tool_calls": [tc("boom")]}])
    router = ToolRouter()

    async def boom() -> None:
        raise RuntimeError("always fails")

    router.register(boom)
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(max_tool_errors=2, max_rounds=10),
    )

    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))
    decision = final_completion(events)
    assert decision["status"] == "failed"
    assert "consecutive tool errors" in decision["reason"]
    # ToolRouter.dispatch catches handler exceptions and returns success=False
    # (it does not raise), so failures surface as failed tool_call_result events,
    # not tool_error. The consecutive-error breaker still trips at the threshold.
    failed_results = [
        e for e in events if e.kind == "tool_call_result" and e.data["success"] is False
    ]
    assert len(failed_results) == 2  # stopped at the threshold, not max_rounds


@pytest.mark.asyncio
async def test_max_rounds_runaway_guard() -> None:
    # Model keeps calling a succeeding tool forever → budget_exhausted at maxRounds.
    provider = make_provider([{"content": "", "tool_calls": [tc("ping")]}])
    router = ToolRouter()

    async def ping() -> str:
        return "pong"

    router.register(ping)
    loop = CoreLoop(provider, RouterToolExecutor(router), LoopConfig(max_rounds=3))

    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))
    decision = final_completion(events)
    assert decision["status"] == "budget_exhausted"
    assert "maxRounds" in decision["reason"]


@pytest.mark.asyncio
async def test_token_budget_exhausted() -> None:
    provider = make_provider(
        [{"content": "hi", "usage": LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)}]
    )
    router = ToolRouter()
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(budget=BudgetLimit(max_tokens=10, max_steps=100, max_tool_calls=100)),
    )

    events = await collect(loop.run([LLMMessage.text_of("user", "hi")]))
    decision = final_completion(events)
    assert decision["status"] == "budget_exhausted"
    assert any(e.kind == "budget_exhausted" and e.data["kind"] == "tokens" for e in events)


@pytest.mark.asyncio
async def test_tool_exception_is_surfaced_not_raised() -> None:
    # An executor that raises must surface as a tool_error event + a failed
    # ToolResult fed back to the model, not crash the loop.
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("explode")]},
            {"content": "recovered"},
        ]
    )

    class _ExplodingExecutor:
        async def execute(self, call: ToolCall, ctx) -> ToolResult:
            raise ValueError("executor blew up")

    loop = CoreLoop(provider, _ExplodingExecutor())
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))
    assert any(e.kind == "tool_error" for e in events)
    # loop recovered and completed on the next turn
    assert final_completion(events)["status"] == "completed"


@pytest.mark.asyncio
async def test_stage_complete_emitted_after_tool_rounds() -> None:
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("echo", {"text": "hi"})]},
            {"content": "done"},
        ]
    )
    router = ToolRouter()

    async def echo(text: str) -> str:
        return text

    router.register(echo)
    loop = CoreLoop(provider, RouterToolExecutor(router))
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    stages = [e for e in events if e.kind == "stage_complete"]
    assert len(stages) == 1  # only the tool round, not the final no-tool round
    assert stages[0].data["round"] == 0
    assert stages[0].data["toolCallCount"] == 1
    assert stages[0].data["consecutiveToolErrors"] == 0
    # ordering: stage_complete comes after the tool result, before completion
    kinds = [e.kind for e in events]
    assert kinds.index("tool_call_result") < kinds.index("stage_complete")
    assert kinds.index("stage_complete") < kinds.index("completion")
