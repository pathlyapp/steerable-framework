"""CoreLoop cooperative cancellation (P1.1).

``loop.cancel()`` winds the run down at the next safe point — round
boundary, stream chunk, or tool-call slot — records the partial turn, and
emits a terminal completion with ``status="cancelled"``. Record semantics:
streamed partial content is kept as the terminal assistant message; issued
tool calls get real results for what ran, a "cancelled" error for what was
in flight, and synthetic skip notices for what never started — the record
never has dangling tool_calls, so the chat can continue afterwards.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_runtime import (
    CoreLoop,
    LoopConfig,
    LoopEvent,
    RouterToolExecutor,
    ToolRouter,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk
from steerable_agent_runtime.storage import InMemoryStorage
from test_loop import collect, final_completion, make_provider, tc


def _slow_provider(parts: list[str]):
    """Provider yielding content parts one event-loop tick apart, so the
    consumer can cancel between chunks."""

    class _Slow:
        name = "slow"
        model = "slow-model"

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(
            self, messages, *, tools=None, **kw
        ) -> AsyncIterator[LLMStreamChunk]:
            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                for part in parts:
                    yield LLMStreamChunk(content_delta=part)
                    await asyncio.sleep(0)
                yield LLMStreamChunk(finish_reason="stop")

            return _gen()

    return _Slow()


def _record_text(message: dict[str, Any]) -> str:
    return "".join(
        p.get("text", "") for p in message.get("content", []) if p.get("type") == "text"
    )


def _tool_messages(record: list[dict[str, Any]]) -> dict[str, str]:
    """call_id -> text for every tool message in the record."""
    out: dict[str, str] = {}
    for entry in record:
        if entry.get("entry") != "item":
            continue
        message = entry["message"]
        if message["role"] == "tool":
            out[message["tool_call_id"]] = _record_text(message)
    return out


def _issued_call_ids(record: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for entry in record:
        if entry.get("entry") != "item":
            continue
        for call in entry["message"].get("tool_calls") or []:
            ids.add(call["id"])
    return ids


@pytest.mark.asyncio
async def test_cancel_before_run_ends_at_first_boundary() -> None:
    provider = make_provider([{"content": "never reached"}])
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()))
    loop.cancel()

    events = await collect(loop.run([LLMMessage.text_of("user", "hi")]))
    decision = final_completion(events)
    assert decision["status"] == "cancelled"
    assert provider.calls == [], "a cancelled run must not issue LLM requests"


@pytest.mark.asyncio
async def test_cancel_mid_stream_records_partial_content() -> None:
    storage = InMemoryStorage()
    provider = _slow_provider(["Hello", " world", "!"])
    loop = CoreLoop(
        provider,
        RouterToolExecutor(ToolRouter()),
        history_store=storage,
        record_id="chat_cancel_stream",
    )

    events: list[LoopEvent] = []
    async for event in loop.run([LLMMessage.text_of("user", "hi")]):
        events.append(event)
        if event.kind == "content_delta":
            loop.cancel()

    decision = final_completion(events)
    assert decision["status"] == "cancelled"
    # The per-chunk check fires before the next delta is processed.
    deltas = [e.data["delta"] for e in events if e.kind == "content_delta"]
    assert deltas == ["Hello"]
    # The partial answer is recorded as the terminal assistant message, so a
    # resume projection sees exactly what the user saw.
    record = await storage.list_history("chat_cancel_stream")
    assistant = [
        e["message"]
        for e in record
        if e.get("entry") == "item" and e["message"]["role"] == "assistant"
    ]
    assert assistant, "partial content was not recorded"
    assert _record_text(assistant[-1]) == "Hello"


@pytest.mark.asyncio
async def test_cancel_at_round_boundary_after_tool_round() -> None:
    router = ToolRouter()

    async def add(a: int, b: int) -> int:
        return a + b

    router.register(add)
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("add", {"a": 1, "b": 2})]},
            {"content": "never reached"},
        ]
    )
    loop = CoreLoop(provider, RouterToolExecutor(router))

    events: list[LoopEvent] = []
    async for event in loop.run([LLMMessage.text_of("user", "add")]):
        events.append(event)
        if event.kind == "tool_call_result":
            loop.cancel()

    decision = final_completion(events)
    assert decision["status"] == "cancelled"
    assert len(provider.calls) == 1, "round 2's LLM request must not happen"


@pytest.mark.asyncio
async def test_cancel_during_sequential_tool_execution() -> None:
    """Write-mode calls form barrier batches of one: cancel lands while the
    first hangs; the second batch must never start."""
    router = ToolRouter()
    first_started = asyncio.Event()
    first_cancelled = False
    second_ran = False

    async def first() -> str:
        nonlocal first_cancelled
        first_started.set()
        try:
            await asyncio.Event().wait()  # hangs until cancelled
        except asyncio.CancelledError:
            first_cancelled = True
            raise
        return "done"  # pragma: no cover

    async def second() -> str:
        nonlocal second_ran
        second_ran = True
        return "two"  # pragma: no cover

    router.register(first, mode="write")
    router.register(second, mode="write")
    storage = InMemoryStorage()
    provider = make_provider(
        [
            {
                "content": "",
                "tool_calls": [tc("first", call_id="c1"), tc("second", call_id="c2")],
            },
            {"content": "never reached"},
        ]
    )
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        history_store=storage,
        record_id="chat_cancel_seq",
    )

    run_task = asyncio.ensure_future(
        collect(loop.run([LLMMessage.text_of("user", "go")]))
    )
    await asyncio.wait_for(first_started.wait(), timeout=2)
    loop.cancel()
    events = await asyncio.wait_for(run_task, timeout=2)

    decision = final_completion(events)
    assert decision["status"] == "cancelled"
    assert first_cancelled, "in-flight tool coroutine was not asyncio-cancelled"
    assert not second_ran, "cancel must stop the turn — second tool never ran"

    # c1 was in flight -> failed result marked cancelled; c2 never started ->
    # synthetic skip notice. Neither dangles.
    record = await storage.list_history("chat_cancel_seq")
    by_call = _tool_messages(record)
    assert "cancelled" in by_call["c1"]
    assert "not executed" in by_call["c2"]


@pytest.mark.asyncio
async def test_cancel_during_parallel_tool_execution() -> None:
    router = ToolRouter()
    both_started = asyncio.Event()
    started_count = 0
    cancelled: list[str] = []

    def _make_tool(tag: str):
        async def _tool() -> str:
            nonlocal started_count
            started_count += 1
            if started_count == 2:
                both_started.set()
            try:
                await asyncio.Event().wait()  # hangs until cancelled
            except asyncio.CancelledError:
                cancelled.append(tag)
                raise
            return tag  # pragma: no cover

        _tool.__name__ = f"read_{tag}"
        return _tool

    router.register(_make_tool("a"))
    router.register(_make_tool("b"))
    storage = InMemoryStorage()
    provider = make_provider(
        [
            {
                "content": "",
                "tool_calls": [tc("read_a", call_id="ca"), tc("read_b", call_id="cb")],
            },
            {"content": "never reached"},
        ]
    )
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(parallel_tools=True),
        history_store=storage,
        record_id="chat_cancel_par",
    )

    run_task = asyncio.ensure_future(
        collect(loop.run([LLMMessage.text_of("user", "go")]))
    )
    await asyncio.wait_for(both_started.wait(), timeout=2)
    loop.cancel()
    events = await asyncio.wait_for(run_task, timeout=2)

    decision = final_completion(events)
    assert decision["status"] == "cancelled"
    assert sorted(cancelled) == ["a", "b"], "both in-flight calls must be cancelled"

    record = await storage.list_history("chat_cancel_par")
    by_call = _tool_messages(record)
    assert "cancelled" in by_call["ca"]
    assert "cancelled" in by_call["cb"]


@pytest.mark.asyncio
async def test_record_has_no_dangling_tool_calls_after_cancel() -> None:
    """Every assistant tool_call id in the record has a matching tool
    message — providers reject requests with dangling calls, so this
    invariant is what makes the post-cancel chat continuable."""
    router = ToolRouter()
    started = asyncio.Event()

    async def hanging(n: int) -> str:
        started.set()
        await asyncio.Event().wait()  # pragma: no cover — cancelled before
        return "x"  # pragma: no cover

    router.register(hanging, mode="write")
    storage = InMemoryStorage()
    provider = make_provider(
        [
            {
                "content": "",
                "tool_calls": [
                    tc("hanging", {"n": 1}, call_id="h1"),
                    tc("hanging", {"n": 2}, call_id="h2"),
                ],
            },
            {"content": "never reached"},
        ]
    )
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        history_store=storage,
        record_id="chat_cancel_dangle",
    )

    run_task = asyncio.ensure_future(
        collect(loop.run([LLMMessage.text_of("user", "go")]))
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    loop.cancel()
    await asyncio.wait_for(run_task, timeout=2)

    record = await storage.list_history("chat_cancel_dangle")
    issued = _issued_call_ids(record)
    answered = set(_tool_messages(record))
    assert issued == {"h1", "h2"}
    assert issued <= answered, f"dangling tool_calls: {issued - answered}"
