"""A1.5c tests — ``HarnessTrace`` persistence via ``StorageAdapter``.

Contract:

* When ``LoopConfig.storage is None``, no extra ``await`` is paid and the
  loop's externally observable behaviour matches A1.5b.
* When a storage is provided, the loop persists:
    - one outer ``loop`` span (``ChatLoop.run``);
    - one ``round`` span per round (parent=loop);
    - one ``llm`` span per round (parent=round, attrs include usage and
      finish reason);
    - one ``tool`` span per dispatched tool call (parent=round, attrs
      include success/error);
    - lifecycle ``loop.start`` / ``loop.end`` events, ``round.start`` /
      ``round.end`` events, ``error`` events, ``budget.exhausted`` events,
      and ``loop.cancelled`` on cancellation.
* The final ``HarnessTrace.status`` matches the loop's final_status
  (``completed`` / ``failed`` / ``budget_exhausted`` / ``cancelled``).
* Storage failures are best-effort: a flaky storage MUST NOT break the
  loop. The trace simply stops growing.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Sequence
from typing import Any

import pytest

from steerable_agent_protocol.generated import (
    HarnessTrace,
    ToolCall,
    TraceEvent,
    TraceSpan,
)
from steerable_agent_runtime import (
    BudgetLimit,
    ChatLoop,
    InMemoryStorage,
    LLMMessage,
    LLMStreamChunk,
    LLMUsage,
    LoopConfig,
    ToolRouter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class ScriptedProvider:
    name = "scripted"
    model = "scripted-model"

    def __init__(self, rounds: list[list[LLMStreamChunk]]) -> None:
        self._rounds = rounds
        self._next = 0

    async def complete(self, *a: Any, **kw: Any) -> tuple[LLMMessage, Any]:
        raise NotImplementedError

    async def stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        idx = self._next
        self._next += 1
        if idx >= len(self._rounds):
            return
        for chunk in self._rounds[idx]:
            yield chunk


class _RaisingStreamProvider:
    name = "raising"
    model = "raising-model"

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def complete(self, *a: Any, **kw: Any) -> tuple[LLMMessage, Any]:
        raise NotImplementedError

    async def stream(
        self,
        messages: Sequence[LLMMessage],
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        raise self._exc
        # pragma: no cover — unreachable
        yield LLMStreamChunk()  # type: ignore[unreachable]


def _text(text: str) -> LLMStreamChunk:
    return LLMStreamChunk(content_delta=text)


def _tc(id: str, name: str, args: dict[str, Any]) -> LLMStreamChunk:
    return LLMStreamChunk(tool_call_delta=ToolCall(id=id, name=name, arguments=args))


def _finish(reason: str = "stop", usage: LLMUsage | None = None) -> LLMStreamChunk:
    return LLMStreamChunk(finish_reason=reason, usage=usage)


def _make_router() -> ToolRouter:
    router = ToolRouter()

    async def echo(text: str = "") -> dict[str, Any]:
        return {"echoed": text}

    async def boom(_: str = "") -> dict[str, Any]:
        raise RuntimeError("boom!")

    router.register(echo, name="echo", description="echo")
    router.register(boom, name="boom", description="raises")
    return router


def _make_loop(
    provider: Any,
    *,
    storage: Any = None,
    budget: BudgetLimit | None = None,
    max_rounds: int = 4,
) -> ChatLoop:
    return ChatLoop(
        LoopConfig(
            provider=provider,
            provider_kind="openai_compat",
            tool_router=_make_router(),
            initial_messages=[LLMMessage(role="user", content="hi")],
            storage=storage,
            budget=budget,
            max_rounds=max_rounds,
        )
    )


async def _drain(loop: ChatLoop) -> None:
    async for _ in loop.run():
        pass


def _span_names_by_kind(spans: list[TraceSpan]) -> dict[str, list[str]]:
    by_kind: dict[str, list[str]] = {}
    for span in spans:
        kind = span.kind or "?"
        by_kind.setdefault(kind, []).append(span.name)
    return by_kind


# ---------------------------------------------------------------------------
# Path 1: storage=None remains a strict no-op (A1.5b parity)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_storage_none_is_a_strict_noop() -> None:
    provider = ScriptedProvider(
        [[_text("hello"), _finish(usage=LLMUsage(5, 7, 12))]]
    )
    loop = _make_loop(provider, storage=None)
    # No exceptions, no extra observable surface.
    await _drain(loop)


# ---------------------------------------------------------------------------
# Path 2: happy-path loop emits a full trace skeleton
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_persists_trace_with_loop_round_llm_spans() -> None:
    storage = InMemoryStorage()
    provider = ScriptedProvider(
        [[_text("hello"), _finish(usage=LLMUsage(5, 7, 12))]]
    )
    loop = _make_loop(provider, storage=storage)
    trace_id = loop.trace_id

    await _drain(loop)

    trace = await storage.get_trace(trace_id)
    assert trace is not None
    assert trace.status == "completed"
    assert trace.hadError is False
    assert trace.modelId == "scripted-model"
    assert trace.totalTokens == 12
    assert trace.durationMs is not None and trace.durationMs >= 0
    assert trace.eventCount > 0
    assert trace.spanCount > 0

    spans = await storage.list_spans(trace_id)
    by_kind = _span_names_by_kind(spans)
    assert "loop" in by_kind and by_kind["loop"] == ["ChatLoop.run"]
    assert by_kind.get("round") == ["round.0"]
    assert by_kind.get("llm") == ["llm_stream"]
    # ``tool`` is absent for a natural-stop turn with no tool_calls.
    assert "tool" not in by_kind


# ---------------------------------------------------------------------------
# Path 3: tool dispatch emits one tool span per call with the correct
# parent + success attribute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_span_persisted_with_parent_round_and_success() -> None:
    storage = InMemoryStorage()
    provider = ScriptedProvider(
        [
            [
                _tc("c1", "echo", {"text": "hi"}),
                _finish(usage=LLMUsage(3, 5, 8)),
            ],
            [_finish(usage=LLMUsage(2, 2, 4))],  # second round: natural stop
        ]
    )
    loop = _make_loop(provider, storage=storage)
    trace_id = loop.trace_id

    await _drain(loop)

    spans = await storage.list_spans(trace_id)
    by_kind = _span_names_by_kind(spans)
    assert by_kind.get("tool") == ["tool:echo"]

    # Verify parent of the tool span is the round.0 span (not the loop span).
    round0_span = next(s for s in spans if s.kind == "round" and s.name == "round.0")
    tool_span = next(s for s in spans if s.kind == "tool")
    assert tool_span.parentSpanId == round0_span.spanId
    assert tool_span.status == "ok"
    assert tool_span.attrs is not None
    assert tool_span.attrs["toolName"] == "echo"
    assert tool_span.attrs["success"] is True


@pytest.mark.asyncio
async def test_tool_call_span_records_failure_with_error_attr() -> None:
    storage = InMemoryStorage()
    provider = ScriptedProvider(
        [
            [_tc("c1", "boom", {}), _finish(usage=LLMUsage(2, 2, 4))],
            [_finish()],
        ]
    )
    loop = _make_loop(provider, storage=storage)
    trace_id = loop.trace_id

    await _drain(loop)

    spans = await storage.list_spans(trace_id)
    tool_span = next(s for s in spans if s.kind == "tool")
    assert tool_span.status == "error"
    assert tool_span.attrs is not None
    assert tool_span.attrs["success"] is False
    # ToolRouter wraps the exception into a fail ToolResult — its ``error``
    # is what lands in the span.
    assert "boom" in (tool_span.attrs.get("error") or "")


# ---------------------------------------------------------------------------
# Path 4: lifecycle events have monotonic sequence + expected vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_vocabulary_and_monotonic_sequence() -> None:
    storage = InMemoryStorage()
    provider = ScriptedProvider(
        [
            [
                _tc("c1", "echo", {"text": "hi"}),
                _finish(usage=LLMUsage(3, 5, 8)),
            ],
            [_finish(usage=LLMUsage(2, 2, 4))],
        ]
    )
    loop = _make_loop(provider, storage=storage)
    trace_id = loop.trace_id

    await _drain(loop)

    events: list[TraceEvent] = await storage.list_events(trace_id)
    seqs = [e.sequence for e in events]
    assert seqs == sorted(seqs), "sequences must be monotonically increasing"

    names = [e.name for e in events]
    # Required envelope.
    assert names[0] == "loop.start"
    assert names[-1] == "loop.end"
    # Round events.
    assert names.count("round.start") == 2
    assert names.count("round.end") == 2


# ---------------------------------------------------------------------------
# Path 5: LLM stream failure marks trace status="failed", hadError=True,
# with an error event recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_failure_marks_trace_failed_and_records_error_event() -> None:
    storage = InMemoryStorage()
    provider = _RaisingStreamProvider(RuntimeError("provider exploded"))
    loop = _make_loop(provider, storage=storage)
    trace_id = loop.trace_id

    await _drain(loop)

    trace = await storage.get_trace(trace_id)
    assert trace is not None
    assert trace.status == "failed"
    assert trace.hadError is True
    assert trace.errorMessage is not None
    assert "provider exploded" in trace.errorMessage

    events = await storage.list_events(trace_id)
    error_events = [e for e in events if e.kind == "error"]
    assert len(error_events) == 1
    assert error_events[0].payload["phase"] == "llm_stream"
    assert error_events[0].payload["errorType"] == "RuntimeError"


# ---------------------------------------------------------------------------
# Path 6: budget exhaustion records a budget.exhausted event with limitKind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_exhausted_records_event_with_limit_kind() -> None:
    storage = InMemoryStorage()
    provider = ScriptedProvider(
        [
            [
                _tc("c1", "echo", {"text": "x"}),
                _finish(usage=LLMUsage(3, 5, 8)),
            ],
            [_tc("c2", "echo", {"text": "y"}), _finish(usage=LLMUsage(3, 5, 8))],
            [_finish()],
        ]
    )
    loop = ChatLoop(
        LoopConfig(
            provider=provider,
            provider_kind="openai_compat",
            tool_router=_make_router(),
            initial_messages=[LLMMessage(role="user", content="hi")],
            storage=storage,
            budget=BudgetLimit(
                max_tokens=1_000_000, max_steps=1, max_tool_calls=1_000
            ),
            max_rounds=5,
        )
    )
    trace_id = loop.trace_id

    await _drain(loop)

    trace = await storage.get_trace(trace_id)
    assert trace is not None
    assert trace.status == "budget_exhausted"

    events = await storage.list_events(trace_id)
    be_events = [e for e in events if e.kind == "budget_exhausted"]
    assert len(be_events) == 1
    assert be_events[0].payload["limitKind"] == "steps"


# ---------------------------------------------------------------------------
# Path 7: cancellation finalises the trace as cancelled
# ---------------------------------------------------------------------------


class _SlowFinishProvider:
    """Yields the first chunk, then awaits forever — guaranteed cancel
    target for cancellation testing."""

    name = "slow"
    model = "slow-model"

    async def complete(self, *a: Any, **kw: Any) -> tuple[LLMMessage, Any]:
        raise NotImplementedError

    async def stream(
        self,
        messages: Sequence[LLMMessage],
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        yield _text("partial-")
        await asyncio.sleep(60)
        # pragma: no cover — never reached, cancellation interrupts
        yield _finish()  # type: ignore[unreachable]


@pytest.mark.asyncio
async def test_cancellation_finalises_trace_with_cancelled_status() -> None:
    storage = InMemoryStorage()
    provider = _SlowFinishProvider()
    loop = _make_loop(provider, storage=storage)
    trace_id = loop.trace_id

    async def consume() -> None:
        async for _ in loop.run():
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    trace = await storage.get_trace(trace_id)
    assert trace is not None
    assert trace.status == "cancelled"
    assert trace.hadError is True
    events = await storage.list_events(trace_id)
    cancel_events = [e for e in events if e.kind == "cancellation"]
    assert len(cancel_events) == 1
    assert cancel_events[0].name == "loop.cancelled"


# ---------------------------------------------------------------------------
# Path 8: a flaky storage MUST NOT break the loop
# ---------------------------------------------------------------------------


class _FlakyStorage:
    """``upsert_trace`` succeeds (so start_loop works), but every other
    write raises. Used to verify the recorder degrades silently."""

    def __init__(self) -> None:
        self.trace: HarnessTrace | None = None
        self.span_calls = 0
        self.event_calls = 0
        self.trace_upsert_calls = 0

    async def upsert_trace(self, trace: HarnessTrace) -> HarnessTrace:
        self.trace_upsert_calls += 1
        self.trace = trace
        return trace

    async def get_trace(self, trace_id: str) -> HarnessTrace | None:
        return self.trace

    async def append_spans(self, trace_id: str, spans: Any) -> None:
        self.span_calls += 1
        raise RuntimeError("simulated span write failure")

    async def append_events(self, trace_id: str, events: Any) -> None:
        self.event_calls += 1
        raise RuntimeError("simulated event write failure")

    async def list_spans(self, trace_id: str) -> list[TraceSpan]:
        return []

    async def list_events(self, trace_id: str) -> list[TraceEvent]:
        return []

    # Unused methods; the recorder only touches the trace + spans + events.
    async def upsert_session(self, *a: Any, **k: Any) -> Any: ...
    async def get_session(self, *a: Any, **k: Any) -> Any: ...
    async def list_sessions(self, *a: Any, **k: Any) -> list[Any]:
        return []

    async def upsert_agent(self, *a: Any, **k: Any) -> Any: ...
    async def get_agent(self, *a: Any, **k: Any) -> Any: ...
    async def list_agents(self, *a: Any, **k: Any) -> list[Any]:
        return []

    async def append_message(self, *a: Any, **k: Any) -> Any: ...
    async def list_messages(self, *a: Any, **k: Any) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_flaky_storage_does_not_break_the_loop() -> None:
    flaky = _FlakyStorage()
    provider = ScriptedProvider(
        [[_text("hi"), _finish(usage=LLMUsage(1, 1, 2))]]
    )
    loop = _make_loop(provider, storage=flaky)

    await _drain(loop)

    # The loop completed normally despite span/event writes failing.
    assert flaky.span_calls >= 1, "we tried to write spans"
    # After the first failure the recorder is disabled — subsequent flushes
    # short-circuit, so we don't expect a runaway call count.
    assert flaky.span_calls <= 2


# ---------------------------------------------------------------------------
# Path 9: HarnessTrace ``createdAt`` is stable across the run
# (created at start, ``updatedAt`` is later)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_created_at_stable_updated_at_later() -> None:
    storage = InMemoryStorage()
    provider = ScriptedProvider([[_text("hi"), _finish()]])
    loop = _make_loop(provider, storage=storage)
    trace_id = loop.trace_id

    await _drain(loop)

    trace = await storage.get_trace(trace_id)
    assert trace is not None
    # ISO-8601 lexicographic comparison is monotonic in time.
    assert trace.createdAt <= trace.updatedAt
