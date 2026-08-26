"""Session resume: project persisted TraceEvents back into an LLM transcript."""

from __future__ import annotations

from typing import Any

import pytest
from steerable_agent_runtime import (
    CoreLoop,
    LoopConfig,
    RouterToolExecutor,
    ToolRouter,
    TraceRecorder,
    load_transcript,
    project_transcript,
)
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.storage import InMemoryStorage

from .test_trace_recorder import make_provider, tc


async def _run_traced(
    script: list[dict[str, Any]],
    router: ToolRouter,
    *,
    config: LoopConfig | None = None,
    max_payload_chars: int = 50_000,
) -> tuple[InMemoryStorage, str]:
    storage = InMemoryStorage()
    recorder = TraceRecorder(storage, max_payload_chars=max_payload_chars)
    loop = CoreLoop(provider=make_provider(script), executor=RouterToolExecutor(router), config=config)
    async for _ in recorder.tee(loop.run([LLMMessage(role="user", content="go")])):
        pass
    return storage, recorder.trace_id


@pytest.mark.asyncio
async def test_project_full_fidelity_roundtrip() -> None:
    """persist_tool_results=True + generous payload budget → the projection
    reproduces the live transcript exactly."""
    router = ToolRouter()

    async def add(a: int, b: int) -> int:
        return a + b

    router.register(add)
    storage, trace_id = await _run_traced(
        [
            {"content": "let me compute. ", "tool_calls": [tc("add", {"a": 1, "b": 2})]},
            {"content": "sum is 3"},
        ],
        router,
        config=LoopConfig(persist_tool_results=True),
    )

    messages = await load_transcript(storage, trace_id)

    assert [m.role for m in messages] == ["assistant", "tool", "assistant"]
    assert messages[0].content == "let me compute. "
    assert messages[0].tool_calls is not None
    assert messages[0].tool_calls[0].name == "add"
    assert messages[0].tool_calls[0].arguments == {"a": 1, "b": 2}
    assert messages[1].tool_call_id == messages[0].tool_calls[0].id
    assert messages[1].name == "add"
    # Same envelope string the live loop put in its transcript
    # (_result_content serializes the ToolResult when no spill applies).
    assert '"value": 3' in messages[1].content
    assert '"success": true' in messages[1].content
    assert messages[2].content == "sum is 3"


@pytest.mark.asyncio
async def test_project_falls_back_to_preview() -> None:
    """Default loop config records only the 300-char preview — projection
    stays lossy-but-usable."""
    router = ToolRouter()

    async def big() -> str:
        return "x" * 1_000

    router.register(big)
    storage, trace_id = await _run_traced(
        [{"content": "", "tool_calls": [tc("big")]}, {"content": "done"}],
        router,
    )

    messages = await load_transcript(storage, trace_id)
    tool_msg = next(m for m in messages if m.role == "tool")
    assert tool_msg.content.endswith("…")
    assert len(tool_msg.content) <= 301


@pytest.mark.asyncio
async def test_projected_transcript_seeds_next_turn() -> None:
    """The projection is a valid prefix: a new loop run on top of it must
    receive the rebuilt history verbatim."""
    router = ToolRouter()

    async def add(a: int, b: int) -> int:
        return a + b

    router.register(add)
    storage, trace_id = await _run_traced(
        [{"content": "", "tool_calls": [tc("add", {"a": 1, "b": 2})]}, {"content": "3"}],
        router,
        config=LoopConfig(persist_tool_results=True),
    )
    history = await load_transcript(storage, trace_id)

    seen_messages: list[LLMMessage] = []
    provider = make_provider([{"content": "follow-up answer"}])
    original_stream = provider.stream

    def capturing_stream(messages, **kw):
        seen_messages.extend(messages)
        return original_stream(messages, **kw)

    provider.stream = capturing_stream  # type: ignore[method-assign]

    followup = CoreLoop(provider=provider, executor=RouterToolExecutor(router))
    async for _ in followup.run([*history, LLMMessage(role="user", content="and now?")]):
        pass

    assert [m.role for m in seen_messages] == [
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert seen_messages[-1].content == "and now?"


@pytest.mark.asyncio
async def test_project_tool_error() -> None:
    router = ToolRouter()

    async def boom() -> None:
        raise RuntimeError("nope")

    router.register(boom)
    storage, trace_id = await _run_traced(
        [{"content": "", "tool_calls": [tc("boom")]}, {"content": "it failed"}],
        router,
    )

    messages = await load_transcript(storage, trace_id)
    tool_msg = next(m for m in messages if m.role == "tool")
    assert "nope" in tool_msg.content
    assert messages[-1].content == "it failed"


def test_project_empty_and_bookkeeping_only() -> None:
    assert project_transcript([]) == []

    class _E:
        def __init__(self, kind: str, payload: dict[str, Any]):
            self.kind = kind
            self.payload = payload

    events = [
        _E("stage_start", {"model": "m"}),
        _E("stage_complete", {"round": 0}),
        _E("completion", {"status": "completed"}),
    ]
    assert project_transcript(events) == []
