"""Terminal tool results: a tool returning ``terminal=True`` ends the turn.

The loop records the tool result, skips any remaining calls, and emits a
terminal completion (status from the tool's declared ``terminal_status``)
instead of feeding the result back for another model round — which would let
the model append a closing note after an interactive card that already said
it (DeepPath's ``ask_user`` regression, 2026-09-13).
"""

from __future__ import annotations

from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall, ToolResult

from steerable_agent_runtime import CoreLoop, LLMMessage
from steerable_agent_runtime.llm import LLMStreamChunk, LLMUsage


def _msg(role: str, text: str) -> LLMMessage:
    return LLMMessage.text_of(role, text)  # type: ignore[arg-type]


def _provider(script: list[dict[str, Any]]):
    class _FakeProvider:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self.calls: list[list[LLMMessage]] = []
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw):
            self.calls.append(list(messages))
            entry = script[min(self._idx, len(script) - 1)]
            self._idx += 1

            async def _gen():
                if entry.get("content"):
                    yield LLMStreamChunk(content_delta=entry["content"])
                for call in entry.get("tool_calls", []):
                    yield LLMStreamChunk(tool_call_delta=call)
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop",
                    usage=LLMUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
                )

            return _gen()

    return _FakeProvider()


class _StubExecutor:
    """Returns a fixed ToolResult for every call (records the calls it saw)."""

    def __init__(self, result: ToolResult) -> None:
        self._result = result
        self.seen: list[str] = []

    async def execute(self, call: ToolCall, ctx: Any) -> ToolResult:
        self.seen.append(call.name)
        return self._result


@pytest.mark.asyncio
async def test_terminal_tool_ends_turn_without_model_continuation() -> None:
    """A terminal tool result ends the turn: the loop emits a terminal
    completion and never offers the result back to the model for a closing
    note."""
    provider = _provider(
        [
            {"tool_calls": [ToolCall(id="q1", name="ask_user", arguments={"intro": "?"})]},
            # A second round must never be consumed — the terminal result ends
            # the turn before the model can append a closing note.
            {"content": "this closing note must never be generated"},
        ]
    )
    executor = _StubExecutor(
        ToolResult(
            success=True,
            terminal=True,
            data={
                "status": "displayed",
                "message": "已向用户展示问题，等待回答",
                "terminal_status": "waiting_user",
            },
        )
    )
    loop = CoreLoop(provider, executor)
    events = [
        e
        async for e in loop.run(
            [_msg("user", "帮我确认")],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "ask_user", "parameters": {"type": "object"}},
                }
            ],
        )
    ]

    assert events[-1].kind == "completion"
    assert events[-1].data["status"] == "waiting_user"
    # The model was never re-invoked after the terminal tool result.
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_terminal_tool_skips_remaining_calls_in_turn() -> None:
    """Calls after a terminal call in the same turn get synthetic skip notices
    (no dangling tool_calls), and none of them execute."""
    provider = _provider(
        [
            {
                "tool_calls": [
                    ToolCall(id="q1", name="ask_user", arguments={"intro": "?"}),
                    ToolCall(id="c2", name="create_event", arguments={"title": "x"}),
                ]
            },
            {"content": "unreachable"},
        ]
    )
    executor = _StubExecutor(
        ToolResult(success=True, terminal=True, data={"terminal_status": "waiting_user"})
    )
    loop = CoreLoop(provider, executor)
    events = [
        e
        async for e in loop.run(
            [_msg("user", "...")],
            tools=[
                {"type": "function", "function": {"name": "ask_user", "parameters": {"type": "object"}}},
                {"type": "function", "function": {"name": "create_event", "parameters": {"type": "object"}}},
            ],
        )
    ]

    assert events[-1].kind == "completion"
    assert events[-1].data["status"] == "waiting_user"
    # Only the terminal call executed; the later call was skipped.
    assert executor.seen == ["ask_user"]
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_non_terminal_result_still_continues_the_turn() -> None:
    """A plain (non-terminal) tool result flows back to the model as before —
    the terminal path must not change the loop's normal act-observe cycle."""
    provider = _provider(
        [
            {"tool_calls": [ToolCall(id="c1", name="search", arguments={"q": "x"})]},
            {"content": "根据搜索结果……"},
        ]
    )
    executor = _StubExecutor(ToolResult(success=True, data={"hits": ["a", "b"]}))
    loop = CoreLoop(provider, executor)
    events = [
        e
        async for e in loop.run(
            [_msg("user", "查一下")],
            tools=[
                {"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}},
            ],
        )
    ]

    assert events[-1].kind == "completion"
    assert events[-1].data["status"] == "completed"
    # The non-terminal result went back to the model for a second round.
    assert len(provider.calls) == 2
