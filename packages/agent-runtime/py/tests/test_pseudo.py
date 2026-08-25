"""Pseudo / markdown tool-call recovery: unit + CoreLoop integration.

Covers the three inline families some models emit as text instead of a
structured tool_calls block (MiniMax XML, DeepSeek XML, Markdown pseudo), and
verifies the loop recovers them into real tool calls that actually execute.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall

from steerable_agent_runtime import (
    CoreLoop,
    LoopEvent,
    RouterToolExecutor,
    ToolRouter,
    extract_inline_tool_calls,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk


def make_provider(script: list[dict[str, Any]]):
    """Fake LLMProvider playing back a scripted sequence of turns."""

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


def tc(name: str, args: dict[str, Any] | None = None, call_id: str | None = None) -> ToolCall:
    return ToolCall(id=call_id or f"call_{name}", name=name, arguments=args or {})


async def collect(loop_run: AsyncIterator[LoopEvent]) -> list[LoopEvent]:
    return [e async for e in loop_run]


def final_completion(events: list[LoopEvent]) -> dict[str, Any]:
    completions = [e for e in events if e.kind == "completion"]
    assert completions, "loop never emitted a completion event"
    return completions[-1].data


def test_extract_minimax_xml() -> None:
    text = '<invoke name="add"><parameter name="a">1</parameter><parameter name="b">2</parameter></invoke>'
    calls, _ = extract_inline_tool_calls(text)
    assert calls == [{"name": "add", "arguments": {"a": "1", "b": "2"}}]


def test_extract_deepseek_xml() -> None:
    text = "<function=add><parameter=a>1</parameter><parameter=b>2</parameter></function>"
    calls, _ = extract_inline_tool_calls(text)
    assert calls == [{"name": "add", "arguments": {"a": "1", "b": "2"}}]


def test_extract_markdown_pseudo_with_json() -> None:
    text = 'Let me check.\n[Tool call: local_exec_shell]\n{"command": "ls -la"}\nDone.'
    calls, cleaned = extract_inline_tool_calls(text)
    assert calls == [{"name": "local_exec_shell", "arguments": {"command": "ls -la"}}]
    # pseudo block removed, surrounding narration survives
    assert "[Tool call:" not in cleaned
    assert "Let me check." in cleaned and "Done." in cleaned


def test_extract_markdown_pseudo_nested_json() -> None:
    text = '[Tool call: search]\n{"query": "x", "opts": {"limit": 3, "tags": ["a"]}}'
    calls, _ = extract_inline_tool_calls(text)
    assert calls[0]["name"] == "search"
    assert calls[0]["arguments"]["opts"] == {"limit": 3, "tags": ["a"]}


def test_extract_markdown_pseudo_no_json() -> None:
    text = "[Tool call: ping]"
    calls, _ = extract_inline_tool_calls(text)
    assert calls == [{"name": "ping", "arguments": {}}]


def test_extract_markdown_pseudo_malformed_json_recovers_empty_args() -> None:
    text = "[Tool call: ping]\n{not valid json"
    calls, _ = extract_inline_tool_calls(text)
    assert calls == [{"name": "ping", "arguments": {}}]


def test_extract_plain_text_returns_no_calls() -> None:
    calls, cleaned = extract_inline_tool_calls("Just a normal answer, no tools.")
    assert calls == []
    assert cleaned == "Just a normal answer, no tools."


@pytest.mark.asyncio
async def test_loop_recovers_markdown_pseudo_and_executes() -> None:
    # Round 1: model emits a markdown pseudo tool-call as *content* (no real
    # tool_calls). Round 2: model gives a final answer. The loop must recover
    # the pseudo call, execute the tool, and feed the observation back.
    provider = make_provider(
        [
            {"content": '[Tool call: add]\n{"a": 1, "b": 2}'},
            {"content": "Sum is 3."},
        ]
    )
    router = ToolRouter()

    async def add(a: int, b: int) -> int:
        return a + b

    router.register(add)
    loop = CoreLoop(provider, RouterToolExecutor(router))

    events = await collect(loop.run([LLMMessage(role="user", content="add")]))

    # the recovered call actually executed
    starts = [e for e in events if e.kind == "tool_call_start"]
    assert len(starts) == 1 and starts[0].data["name"] == "add"
    results = [e for e in events if e.kind == "tool_call_result"]
    assert len(results) == 1 and results[0].data["success"] is True

    # the second LLM call saw the tool observation
    second_call_messages = provider.calls[1]
    tool_msgs = [m for m in second_call_messages if m.role == "tool"]
    assert len(tool_msgs) == 1 and tool_msgs[0].name == "add"

    assert final_completion(events)["status"] == "completed"


@pytest.mark.asyncio
async def test_loop_recovers_xml_pseudo_and_executes() -> None:
    provider = make_provider(
        [
            {"content": '<invoke name="ping"><parameter name="x">1</parameter></invoke>'},
            {"content": "pong"},
        ]
    )
    router = ToolRouter()

    async def ping(x: str) -> str:
        return f"pong:{x}"

    router.register(ping)
    loop = CoreLoop(provider, RouterToolExecutor(router))

    events = await collect(loop.run([LLMMessage(role="user", content="go")]))
    starts = [e for e in events if e.kind == "tool_call_start"]
    assert len(starts) == 1 and starts[0].data["name"] == "ping"
    assert final_completion(events)["status"] == "completed"


@pytest.mark.asyncio
async def test_loop_does_not_recover_when_real_tool_calls_present() -> None:
    # If the round already produced real tool_calls, recovery must not fire —
    # the pseudo-looking text stays as content and is not double-executed.
    provider = make_provider(
        [
            {
                "content": "calling now",
                "tool_calls": [tc("add", {"a": 1, "b": 2})],
            },
            {"content": "done"},
        ]
    )
    router = ToolRouter()

    async def add(a: int, b: int) -> int:
        return a + b

    router.register(add)
    loop = CoreLoop(provider, RouterToolExecutor(router))

    events = await collect(loop.run([LLMMessage(role="user", content="add")]))
    starts = [e for e in events if e.kind == "tool_call_start"]
    # exactly one execution (the real call), no recovered duplicate
    assert len(starts) == 1
    assert final_completion(events)["status"] == "completed"


@pytest.mark.asyncio
async def test_loop_plain_text_still_completes_without_recovery() -> None:
    provider = make_provider([{"content": "The answer is 4."}])
    router = ToolRouter()
    loop = CoreLoop(provider, RouterToolExecutor(router))

    events = await collect(loop.run([LLMMessage(role="user", content="2+2?")]))
    assert final_completion(events)["status"] == "completed"
    # no tool was executed
    assert not [e for e in events if e.kind == "tool_call_start"]
