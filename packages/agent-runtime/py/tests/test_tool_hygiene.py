"""Tool hygiene: dedup guard, unknown-tool suggestions, argument coercion."""

from __future__ import annotations

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
                for call in entry.get("tool_calls", []):
                    yield LLMStreamChunk(tool_call_delta=call)
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop"
                )

            return _gen()

    return _FakeProvider()


def tc(name: str, args: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(id=f"call_{name}_{len(name)}_{abs(hash(str(args))) % 1000}",
                    name=name, arguments=args or {})


async def collect(loop_run: AsyncIterator[LoopEvent]) -> list[LoopEvent]:
    return [e async for e in loop_run]


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_call_is_blocked_without_executing() -> None:
    # The model repeats the exact same (name, args) in consecutive rounds;
    # the second must be blocked as duplicate_call and the tool runs once.
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("get_weather", {"city": "Berlin"})]},
            {"content": "", "tool_calls": [tc("get_weather", {"city": "Berlin"})]},
            {"content": "It is sunny."},
        ]
    )
    router = ToolRouter()
    executions: list[dict] = []

    async def get_weather(city: str) -> str:
        executions.append({"city": city})
        return "sunny"

    router.register(get_weather)
    loop = CoreLoop(provider, RouterToolExecutor(router))
    events = await collect(loop.run([LLMMessage(role="user", content="weather?")]))

    assert executions == [{"city": "Berlin"}]  # ran exactly once
    results = [e for e in events if e.kind == "tool_call_result"]
    assert len(results) == 2
    assert results[0].data["success"] is True
    assert results[1].data["success"] is False
    # the model sees the soft signal in the transcript
    tool_msgs = [m for m in provider.calls[2] if m.role == "tool"]
    assert "duplicate_call" in tool_msgs[-1].content
    assert events[-1].data["status"] == "completed"


@pytest.mark.asyncio
async def test_same_tool_different_args_not_blocked() -> None:
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("get_weather", {"city": "Berlin"})]},
            {"content": "", "tool_calls": [tc("get_weather", {"city": "Paris"})]},
            {"content": "both sunny"},
        ]
    )
    router = ToolRouter()
    executions: list[str] = []

    async def get_weather(city: str) -> str:
        executions.append(city)
        return "sunny"

    router.register(get_weather)
    loop = CoreLoop(provider, RouterToolExecutor(router))
    await collect(loop.run([LLMMessage(role="user", content="weather?")]))

    assert executions == ["Berlin", "Paris"]


@pytest.mark.asyncio
async def test_dedup_can_be_disabled() -> None:
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("poll", {"id": 1})]},
            {"content": "", "tool_calls": [tc("poll", {"id": 1})]},
            {"content": "done"},
        ]
    )
    router = ToolRouter()
    executions: list[int] = []

    async def poll(id: int) -> str:
        executions.append(id)
        return "pending"

    router.register(poll)
    loop = CoreLoop(
        provider, RouterToolExecutor(router), LoopConfig(tool_dedup=False)
    )
    await collect(loop.run([LLMMessage(role="user", content="poll")]))

    assert executions == [1, 1]


# ---------------------------------------------------------------------------
# Unknown tool suggestion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_tool_returns_suggestions() -> None:
    router = ToolRouter()

    async def list_tasks() -> list:
        return []

    async def list_projects() -> list:
        return []

    router.register(list_tasks)
    router.register(list_projects)

    result = await router.dispatch(ToolCall(id="c1", name="list_task", arguments={}))
    assert result.success is False
    assert "list_tasks" in result.error
    # closest match first; the permissive cutoff may recall more candidates
    assert result.data["suggestions"][0] == "list_tasks"
    assert result.needsFollowup is True


@pytest.mark.asyncio
async def test_unknown_tool_without_close_match_lists_valid_tools() -> None:
    router = ToolRouter()

    async def list_tasks() -> list:
        return []

    router.register(list_tasks)

    result = await router.dispatch(ToolCall(id="c2", name="xyzzy", arguments={}))
    assert result.success is False
    assert result.error == "Unknown tool: xyzzy. Available tools: list_tasks"
    assert result.data["suggestions"] == []


# ---------------------------------------------------------------------------
# Argument coercion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arguments_coerced_to_schema_types() -> None:
    router = ToolRouter()
    seen: list[dict] = []

    async def create_task(title: str, priority: int, weight: float) -> str:
        seen.append({"title": title, "priority": priority, "weight": weight})
        return "ok"

    router.register(
        create_task,
        schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "priority": {"type": "integer"},
                "weight": {"type": "number"},
            },
        },
    )

    result = await router.dispatch(
        ToolCall(
            id="c3",
            name="create_task",
            # model returned wrong primitives: int for string, str for int…
            arguments={"title": 11111, "priority": "3", "weight": "0.5"},
        )
    )
    assert result.success is True
    assert seen == [{"title": "11111", "priority": 3, "weight": 0.5}]


@pytest.mark.asyncio
async def test_uncoercible_values_are_left_as_is() -> None:
    router = ToolRouter()
    seen: list[dict] = []

    async def set_count(count: int) -> str:
        seen.append({"count": count})
        return "ok"

    router.register(
        set_count,
        schema={"type": "object", "properties": {"count": {"type": "integer"}}},
    )

    result = await router.dispatch(
        ToolCall(id="c4", name="set_count", arguments={"count": "not-a-number"})
    )
    # left as-is; the handler receives the original (and may itself fail)
    assert seen == [{"count": "not-a-number"}]
    assert result.success is True
