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
    # W2.7.2 span model: one llm span per provider request (2 rounds) plus
    # one tool span.
    assert trace.spanCount == 3
    assert trace.eventCount == len(seen)

    spans = await storage.list_spans(recorder.trace_id)
    llm_spans = [s for s in spans if s.kind == "llm"]
    tool_spans = [s for s in spans if s.kind == "tool"]
    assert len(llm_spans) == 2
    assert [s.name for s in llm_spans] == ["llm.request", "llm.request"]
    assert [s.attrs["round"] for s in llm_spans] == [0, 1]  # type: ignore[index]
    assert len(tool_spans) == 1
    assert tool_spans[0].name == "add"
    assert tool_spans[0].status == "ok"
    assert tool_spans[0].durationMs is not None

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
    tool_spans = [s for s in spans if s.kind == "tool"]
    assert len(tool_spans) == 1
    assert tool_spans[0].status == "error"
    assert "nope" in (tool_spans[0].attrs or {}).get("error", "")


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
async def test_secrets_never_enter_the_trace() -> None:
    """Mechanical gate (spec "Secret redaction"): a tool that returns a live
    credential — and a tool argument that *is* one — must not appear anywhere
    in the persisted trace events or spans."""
    secret_key = "sk-live0000000000000000deadbeef"
    secret_token = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig"

    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("fetch", {"api_key": secret_key, "url": "https://x"})]},
            {"content": "done"},
        ]
    )
    router = ToolRouter()

    async def fetch(api_key: str, url: str) -> dict[str, Any]:
        # Echo the credential back in the result, as a leaky tool would.
        return {"authorization": secret_token, "echo": api_key, "body": "ok"}

    router.register(fetch)
    storage = InMemoryStorage()
    recorder = TraceRecorder(storage)
    loop = CoreLoop(provider, RouterToolExecutor(router))

    async for _ in recorder.tee(loop.run([LLMMessage.text_of("user", "go")])):
        pass

    events = await storage.list_events(recorder.trace_id)
    spans = await storage.list_spans(recorder.trace_id)
    import json

    blob = json.dumps(
        {
            "events": [e.payload for e in events],
            "spans": [s.attrs for s in spans],
        }
    )
    assert secret_key not in blob
    assert secret_token not in blob
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in blob


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


@pytest.mark.asyncio
async def test_recorder_llm_retry_produces_one_span_per_attempt() -> None:
    """A retried provider call yields two llm spans: attempt 1 errored,
    attempt 2 ok — the real request count is visible, not collapsed."""

    class _FlakyProvider:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self._calls = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            self._calls += 1
            calls = self._calls

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                if calls == 1:
                    raise RuntimeError("connection reset")
                yield LLMStreamChunk(content_delta="recovered")

            return _gen()

    from steerable_agent_runtime.hooks import NoopHooks, RetryAction

    class _RetryHooks(NoopHooks):
        async def on_request_error(self, exc, messages, ctx):
            return RetryAction(kind="retry", reason="transient")

    storage = InMemoryStorage()
    recorder = TraceRecorder(storage)
    loop = CoreLoop(_FlakyProvider(), RouterToolExecutor(ToolRouter()), hooks=_RetryHooks())
    async for _ in recorder.tee(loop.run([LLMMessage.text_of("user", "hi")])):
        pass

    spans = await storage.list_spans(recorder.trace_id)
    llm = [s for s in spans if s.kind == "llm"]
    assert len(llm) == 2
    assert llm[0].status == "error"
    assert "connection reset" in (llm[0].attrs or {}).get("error", "")
    assert llm[1].status == "ok"
    assert [s.attrs["attempt"] for s in llm] == [1, 2]  # type: ignore[index]


@pytest.mark.asyncio
async def test_recorder_approval_wait_span_parented_to_tool() -> None:
    """An interactive approval becomes an approval.wait span bracketing the
    wait, parented to the tool span — approval latency is attributable."""
    import asyncio as _asyncio

    from steerable_agent_runtime import ApprovalDecision, ApprovalExecutor

    class _SlowApprover:
        async def approve(self, request):
            await _asyncio.sleep(0.05)
            return ApprovalDecision("allow_once", "ok")

    provider = make_provider(
        [{"content": "", "tool_calls": [tc("add", {"a": 1, "b": 2})]}, {"content": "3"}]
    )
    router = ToolRouter()

    async def add(a: int, b: int) -> int:
        return a + b

    router.register(add)
    storage = InMemoryStorage()
    recorder = TraceRecorder(storage)
    loop = CoreLoop(
        provider,
        ApprovalExecutor(RouterToolExecutor(router), _SlowApprover()),
    )
    async for _ in recorder.tee(loop.run([LLMMessage.text_of("user", "add")])):
        pass

    spans = await storage.list_spans(recorder.trace_id)
    approval = [s for s in spans if s.kind == "approval"]
    tool = [s for s in spans if s.kind == "tool"]
    assert len(approval) == 1 and len(tool) == 1
    assert approval[0].name == "approval.wait"
    assert approval[0].parentSpanId == tool[0].spanId
    assert (approval[0].durationMs or 0) >= 40
    assert approval[0].attrs["kind"] == "allow_once"  # type: ignore[index]


@pytest.mark.asyncio
async def test_recorder_sampling_deterministic_and_drops_persistence() -> None:
    """sample_rate=0 records nothing but events still tee through; the
    sampling decision is a deterministic function of the trace id."""
    provider = make_provider([{"content": "hi"}])
    storage = InMemoryStorage()
    recorder = TraceRecorder(storage, trace_id="trace_x", sample_rate=0.0)
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()))

    seen: list[LoopEvent] = []
    async for event in recorder.tee(loop.run([LLMMessage.text_of("user", "hi")])):
        seen.append(event)

    assert seen[-1].data["status"] == "completed"
    assert await storage.get_trace("trace_x") is None
    assert await storage.list_events("trace_x") == []
    # finalize still returns a summary object without persisting
    summary = await recorder.finalize()
    assert summary.traceId == "trace_x"

    # Determinism: same trace id → same decision
    r1 = TraceRecorder(storage, trace_id="trace_x", sample_rate=0.5)
    r2 = TraceRecorder(storage, trace_id="trace_x", sample_rate=0.5)
    assert r1._sampled == r2._sampled

    with pytest.raises(ValueError):
        TraceRecorder(storage, sample_rate=1.5)
