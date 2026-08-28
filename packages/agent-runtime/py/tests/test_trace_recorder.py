"""TraceRecorder: tee the loop event stream into a StorageAdapter."""

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
    TraceRecorder,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk
from steerable_agent_runtime.storage import InMemoryStorage


def make_provider(script: list[dict[str, Any]]):
    class _FakeProvider:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
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
    return ToolCall(id=f"call_{name}_{abs(hash(str(args))) % 1000}", name=name, arguments=args or {})


@pytest.mark.asyncio
async def test_recorder_persists_events_spans_and_trace() -> None:
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("add", {"a": 1, "b": 2})]},
            {"content": "sum is 3"},
        ]
    )
    router = ToolRouter()

    async def add(a: int, b: int) -> int:
        return a + b

    router.register(add)
    storage = InMemoryStorage()
    recorder = TraceRecorder(storage, chat_id="chat_1")
    loop = CoreLoop(provider, RouterToolExecutor(router))

    seen: list[LoopEvent] = []
    async for event in recorder.tee(loop.run([LLMMessage.text_of("user", "add")])):
        seen.append(event)

    # events flowed through untouched
    assert seen[-1].data["status"] == "completed"

    trace = await storage.get_trace(recorder.trace_id)
    assert trace is not None
    assert trace.status == "completed"
    assert trace.hadError is False
    assert trace.chatId == "chat_1"
    assert trace.spanCount == 1
    assert trace.eventCount == len(seen)

    spans = await storage.list_spans(recorder.trace_id)
    assert len(spans) == 1
    assert spans[0].name == "add"
    assert spans[0].status == "ok"
    assert spans[0].durationMs is not None

    events = await storage.list_events(recorder.trace_id)
    kinds = [e.kind for e in events]
    assert kinds[0] == "stage_start"
    assert "tool_call_start" in kinds
    assert "tool_call_result" in kinds
    assert kinds[-1] == "completion"
    # sequences are strictly increasing
    seqs = [e.sequence for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


@pytest.mark.asyncio
async def test_recorder_marks_failed_tool_span() -> None:
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("boom")]},
            {"content": "it failed"},
        ]
    )
    router = ToolRouter()

    async def boom() -> None:
        raise RuntimeError("nope")

    router.register(boom)
    storage = InMemoryStorage()
    recorder = TraceRecorder(storage)
    loop = CoreLoop(provider, RouterToolExecutor(router))

    async for _ in recorder.tee(loop.run([LLMMessage.text_of("user", "go")])):
        pass

    trace = await storage.get_trace(recorder.trace_id)
    assert trace is not None and trace.hadError is True
    spans = await storage.list_spans(recorder.trace_id)
    assert spans[0].status == "error"
    assert "nope" in (spans[0].attrs or {}).get("error", "")


@pytest.mark.asyncio
async def test_recorder_truncates_huge_payloads() -> None:
    provider = make_provider([{"content": "x" * 10_000}])
    storage = InMemoryStorage()
    recorder = TraceRecorder(storage, max_payload_chars=100)
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()))

    async for _ in recorder.tee(loop.run([LLMMessage.text_of("user", "hi")])):
        pass

    events = await storage.list_events(recorder.trace_id)
    for e in events:
        for value in (e.payload or {}).values():
            assert not isinstance(value, str) or len(value) <= 101


@pytest.mark.asyncio
async def test_trace_row_is_live_mid_turn() -> None:
    """The trace row is upserted status=running on the first event, so
    trace.fetch works while the turn is still in flight; finalize overwrites
    with the terminal status."""
    provider = make_provider([{"content": "hi"}])
    storage = InMemoryStorage()
    recorder = TraceRecorder(storage)
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()))

    seen_running = None
    async for event in recorder.tee(loop.run([LLMMessage.text_of("user", "go")])):
        if seen_running is None:
            mid = await storage.get_trace(recorder.trace_id)
            assert mid is not None
            seen_running = mid.status
    assert seen_running == "running"

    final = await storage.get_trace(recorder.trace_id)
    assert final is not None
    assert final.status == "completed"
    assert final.createdAt <= final.updatedAt
