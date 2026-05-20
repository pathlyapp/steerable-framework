"""A1.4 tests — the four hooks left dormant after A1.2/A1.3 are now live:

* ``emit`` — every ``SSEEvent`` is funneled through ``_emit()``; callbacks can
  rewrite the event (return new ``SSEEvent``), mutate ``ctx.event`` in place,
  or return ``HOOK_SKIP`` to suppress emission entirely.
* ``error`` — LLM-stream errors are fatal (``final_status="failed"``); tool-
  dispatch errors are recoverable (``decide_completion`` decides).
* ``budget_exhausted`` — fires on each of four exhaustion paths
  (step / tokens / tool_calls / rounds), and a matching SSE event is emitted.
* ``CancelledError`` — caller cancels the task running ``run()``; the loop
  fires ``loop_end(final_status="cancelled")``, emits a final
  ``agent.event=session.end``, and re-raises.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Sequence
from typing import Any

import pytest

from steerable_agent_protocol.generated import SSEEvent, ToolCall, ToolResult
from steerable_agent_runtime import (
    HOOK_SKIP,
    AssistantMessageCtx,
    BudgetExhaustedCtx,
    BudgetLimit,
    ChatLoop,
    EmitCtx,
    ErrorCtx,
    LLMMessage,
    LLMStreamChunk,
    LLMUsage,
    LoopConfig,
    LoopEndCtx,
    ToolRouter,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


class ScriptedProvider:
    name = "scripted"
    model = "scripted-model"

    def __init__(self, rounds: list[list[LLMStreamChunk]]) -> None:
        self._rounds = rounds
        self._next = 0
        self.calls: list[dict[str, Any]] = []

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
        self.calls.append({"messages": list(messages)})
        if idx >= len(self._rounds):
            return
        for chunk in self._rounds[idx]:
            yield chunk


class _RaisingStreamProvider:
    """First ``stream`` call raises; subsequent (defensively) returns empty."""

    name = "raising"
    model = "raising-model"

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self._calls = 0

    async def complete(self, *a: Any, **kw: Any) -> tuple[LLMMessage, Any]:
        raise NotImplementedError

    async def stream(
        self,
        messages: Sequence[LLMMessage],
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        self._calls += 1
        if self._calls == 1:
            raise self._exc
        return
        # pragma: no cover — unreachable, kept to advertise the type
        yield LLMStreamChunk()  # type: ignore[unreachable]


def _text(text: str) -> LLMStreamChunk:
    return LLMStreamChunk(content_delta=text)


def _tc(id: str, name: str, args: dict[str, Any]) -> LLMStreamChunk:
    return LLMStreamChunk(tool_call_delta=ToolCall(id=id, name=name, arguments=args))


def _finish(reason: str = "stop", usage: LLMUsage | None = None) -> LLMStreamChunk:
    return LLMStreamChunk(finish_reason=reason, usage=usage)


def _make_router() -> tuple[ToolRouter, dict[str, Any]]:
    obs: dict[str, Any] = {"calls": []}
    router = ToolRouter()

    async def echo(text: str = "") -> dict[str, Any]:
        obs["calls"].append(("echo", text))
        return {"echoed": text}

    async def explode() -> ToolResult:
        obs["calls"].append(("explode",))
        raise RuntimeError("kaboom in tool")

    router.register(echo, description="Echo")
    router.register(explode, description="Always raises")
    return router, obs


def _make_config(
    provider: Any,
    router: ToolRouter,
    *,
    budget: BudgetLimit | None = None,
    max_rounds: int = 12,
) -> LoopConfig:
    return LoopConfig(
        provider=provider,
        provider_kind="openai_compat",
        tool_router=router,
        initial_messages=[LLMMessage(role="user", content="go")],
        budget=budget,
        max_rounds=max_rounds,
    )


async def _collect(loop: ChatLoop) -> list[SSEEvent]:
    return [ev async for ev in loop.run()]


# ---------------------------------------------------------------------------
# emit hook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_hook_sees_all_events_in_order() -> None:
    """With a trivial natural-stop run the hook sees exactly the three
    session-envelope events: session.start → done → session.end."""
    router, _ = _make_router()
    provider = ScriptedProvider(rounds=[[_finish("stop")]])
    loop = ChatLoop(_make_config(provider, router))

    seen: list[tuple[str, str | None]] = []

    async def cb(ctx: EmitCtx) -> None:
        seen.append((ctx.event.type, ctx.event.event))

    loop.on("emit", cb)
    await _collect(loop)

    assert seen == [
        ("agent", "session.start"),
        ("done", None),
        ("agent", "session.end"),
    ]


@pytest.mark.asyncio
async def test_emit_hook_in_place_mutation_propagates_to_consumer() -> None:
    """Hook mutates ``ctx.event.payload`` in place; downstream consumer sees
    the mutated event."""
    router, _ = _make_router()
    provider = ScriptedProvider(rounds=[[_finish("stop")]])
    loop = ChatLoop(_make_config(provider, router))

    async def cb(ctx: EmitCtx) -> None:
        if ctx.event.event == "session.start":
            ctx.event.payload = {**(ctx.event.payload or {}), "marker": "mutated"}

    loop.on("emit", cb)
    events = await _collect(loop)

    start = next(e for e in events if e.event == "session.start")
    assert start.payload is not None
    assert start.payload["marker"] == "mutated"


@pytest.mark.asyncio
async def test_emit_hook_can_return_replacement_event() -> None:
    """If the hook returns an ``SSEEvent``, that one is yielded instead."""
    router, _ = _make_router()
    provider = ScriptedProvider(rounds=[[_finish("stop")]])
    loop = ChatLoop(_make_config(provider, router))

    async def cb(ctx: EmitCtx) -> SSEEvent | None:
        if ctx.event.type == "done":
            return SSEEvent(type="done", payload={"rewritten": True})
        return None

    loop.on("emit", cb)
    events = await _collect(loop)

    done = next(e for e in events if e.type == "done")
    assert done.payload == {"rewritten": True}


@pytest.mark.asyncio
async def test_emit_hook_hook_skip_suppresses_event() -> None:
    """``HOOK_SKIP`` drops the event from the stream entirely; the rest of
    the envelope still emits cleanly."""
    router, _ = _make_router()
    provider = ScriptedProvider(rounds=[[_finish("stop")]])
    loop = ChatLoop(_make_config(provider, router))

    async def cb(ctx: EmitCtx) -> Any:
        if ctx.event.type == "done":
            return HOOK_SKIP
        return None

    loop.on("emit", cb)
    events = await _collect(loop)

    types = [(e.type, e.event) for e in events]
    assert ("done", None) not in types
    # session.start + session.end remain
    assert types == [("agent", "session.start"), ("agent", "session.end")]


@pytest.mark.asyncio
async def test_emit_hooks_compose_in_registration_order() -> None:
    """Two emit hooks: the first mutates ``ctx.event.payload`` in place, the
    second reads the (already-mutated) ctx and returns a replacement
    ``SSEEvent``. The replacement wins, and it carries the first hook's
    mutation alongside its own (plus the loop-supplied sessionId/traceId)."""
    router, _ = _make_router()
    provider = ScriptedProvider(rounds=[[_finish("stop")]])
    loop = ChatLoop(_make_config(provider, router))

    async def first(ctx: EmitCtx) -> None:
        if ctx.event.event == "session.start":
            ctx.event.payload = {**(ctx.event.payload or {}), "via_first": True}

    async def second(ctx: EmitCtx) -> SSEEvent | None:
        if ctx.event.event == "session.start":
            return SSEEvent(
                type="agent",
                event="session.start",
                payload={**(ctx.event.payload or {}), "via_second": True},
            )
        return None

    loop.on("emit", first)
    loop.on("emit", second)
    events = await _collect(loop)
    start = next(e for e in events if e.event == "session.start")
    payload = start.payload or {}
    assert payload.get("via_first") is True  # first hook's in-place mutation
    assert payload.get("via_second") is True  # second hook's replacement
    assert "sessionId" in payload  # original loop-supplied keys survive


# ---------------------------------------------------------------------------
# error hook — LLM stream (fatal)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_stream_error_is_fatal_and_emits_error_event() -> None:
    router, _ = _make_router()
    provider = _RaisingStreamProvider(RuntimeError("model fell over"))
    loop = ChatLoop(_make_config(provider, router))

    error_calls: list[ErrorCtx] = []
    end_calls: list[LoopEndCtx] = []

    async def on_error(ctx: ErrorCtx) -> None:
        error_calls.append(ctx)

    async def on_end(ctx: LoopEndCtx) -> None:
        end_calls.append(ctx)

    loop.on("error", on_error)
    loop.on("loop_end", on_end)

    events = await _collect(loop)

    # 1. error hook fired once with phase=llm_stream
    assert len(error_calls) == 1
    assert error_calls[0].phase == "llm_stream"
    assert isinstance(error_calls[0].exception, RuntimeError)

    # 2. SSE envelope: error event present, plus session.start / done / session.end
    types = [(e.type, e.event) for e in events]
    assert ("agent", "error") in types
    assert ("agent", "session.start") in types
    assert ("done", None) in types
    assert ("agent", "session.end") in types

    # 3. loop_end fired with final_status=failed, with a decision payload
    assert len(end_calls) == 1
    end = end_calls[0]
    assert end.final_status == "failed"
    assert end.final_decision is not None
    assert end.final_decision["status"] == "failed"
    assert end.final_decision["reason"].startswith("llm_stream_exception:")


@pytest.mark.asyncio
async def test_llm_stream_error_event_carries_diagnostic_payload() -> None:
    router, _ = _make_router()
    provider = _RaisingStreamProvider(ValueError("bad token"))
    loop = ChatLoop(_make_config(provider, router))

    events = await _collect(loop)
    err = next(e for e in events if e.event == "error")
    assert err.payload is not None
    assert err.payload["phase"] == "llm_stream"
    assert err.payload["errorType"] == "ValueError"
    assert err.payload["message"] == "bad token"
    assert err.payload["roundIndex"] == 0


# ---------------------------------------------------------------------------
# error hook — tool dispatch (recoverable)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_dispatch_infrastructure_error_fires_hook_and_continues() -> None:
    """``error`` hook on the ``tool_dispatch`` phase fires when the ``ToolRouter``
    itself crashes (loop's safety-net ``except``). The loop synthesises a fail
    ``ToolResult`` and continues; the next round's ``decide_completion`` makes
    the call about whether to stop or proceed.

    Note: a *business* tool raising (e.g. ``RuntimeError("kaboom")``) does NOT
    reach this path — ``ToolRouter.dispatch`` catches and wraps the exception
    into ``ToolResult(success=False, error=...)`` before the loop sees it. The
    ``error`` hook is reserved for "framework infrastructure broke", not "the
    user's tool returned a failure" (which is handled via ``after_tool_result``
    inspecting ``result.error``). This test patches ``dispatch`` directly to
    simulate the rare infrastructure failure.
    """
    router = ToolRouter()

    async def echo(text: str = "") -> dict[str, Any]:
        return {"echoed": text}

    router.register(echo, description="Echo")

    async def crashing_dispatch(call: ToolCall) -> ToolResult:
        raise RuntimeError("router internal failure")

    router.dispatch = crashing_dispatch  # type: ignore[method-assign]

    provider = ScriptedProvider(
        rounds=[
            [
                _tc("c1", "echo", {"text": "x"}),
                _finish("tool_calls"),
            ],
            [_finish("stop")],  # round 1: natural stop
        ]
    )
    loop = ChatLoop(_make_config(provider, router))

    errors: list[ErrorCtx] = []
    ends: list[LoopEndCtx] = []

    async def on_error(ctx: ErrorCtx) -> None:
        errors.append(ctx)

    async def on_end(ctx: LoopEndCtx) -> None:
        ends.append(ctx)

    loop.on("error", on_error)
    loop.on("loop_end", on_end)

    events = await _collect(loop)

    # 1. error hook fired once with phase=tool_dispatch
    assert len(errors) == 1
    assert errors[0].phase == "tool_dispatch"
    assert isinstance(errors[0].exception, RuntimeError)
    assert "router internal failure" in str(errors[0].exception)

    # 2. error SSE emitted
    error_events = [e for e in events if e.event == "error"]
    assert len(error_events) == 1
    assert error_events[0].payload is not None
    assert error_events[0].payload["phase"] == "tool_dispatch"

    # 3. loop continued — natural-stop completed, not failed
    assert len(ends) == 1
    assert ends[0].final_status == "completed"
    assert ends[0].rounds_completed == 2


@pytest.mark.asyncio
async def test_business_tool_exception_does_not_fire_error_hook() -> None:
    """Symmetric guard: a business tool raising ``RuntimeError`` goes through
    ``ToolRouter.dispatch``'s wrap-as-fail-result path. The ``error`` hook is
    NOT supposed to fire — ``after_tool_result`` is where downstream inspects
    ``result.error``. This test pins that contract so future refactors don't
    accidentally start double-reporting business failures."""
    router, _ = _make_router()
    provider = ScriptedProvider(
        rounds=[
            [
                _tc("c1", "explode", {}),
                _finish("tool_calls"),
            ],
            [_finish("stop")],
        ]
    )
    loop = ChatLoop(_make_config(provider, router))

    errors: list[ErrorCtx] = []
    result_errors: list[str | None] = []

    async def on_error(ctx: ErrorCtx) -> None:
        errors.append(ctx)

    async def on_tool_result(ctx: Any) -> None:
        result_errors.append(ctx.tool_result.error)

    loop.on("error", on_error)
    loop.on("after_tool_result", on_tool_result)

    await _collect(loop)

    # No error hook fired — ToolRouter wrapped it transparently.
    assert errors == []
    # But after_tool_result *did* see the failure via result.error.
    assert any(err and "kaboom" in err for err in result_errors)


# ---------------------------------------------------------------------------
# budget_exhausted hook (4 paths)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_exhausted_hook_on_steps_path() -> None:
    """``max_steps=1`` makes round 1's pre-debit trip the cap; the hook fires
    with ``limit_kind="steps"`` and an SSE event is emitted."""
    router, _ = _make_router()
    provider = ScriptedProvider(
        rounds=[
            [_tc("c1", "echo", {"text": "x"}), _finish("tool_calls")],
            [_finish("stop")],
        ]
    )
    loop = ChatLoop(
        _make_config(
            provider,
            router,
            budget=BudgetLimit(max_tokens=1_000_000, max_steps=1, max_tool_calls=1_000),
        )
    )

    hook_calls: list[BudgetExhaustedCtx] = []

    async def cb(ctx: BudgetExhaustedCtx) -> None:
        hook_calls.append(ctx)

    loop.on("budget_exhausted", cb)
    events = await _collect(loop)

    assert len(hook_calls) == 1
    assert hook_calls[0].limit_kind == "steps"
    assert hook_calls[0].budget_state is not None
    assert hook_calls[0].budget_state["steps_used"] == 2

    be_events = [e for e in events if e.event == "budget_exhausted"]
    assert len(be_events) == 1
    assert be_events[0].payload is not None
    assert be_events[0].payload["limitKind"] == "steps"
    assert be_events[0].payload["budgetState"]["steps_used"] == 2


@pytest.mark.asyncio
async def test_budget_exhausted_hook_on_tokens_path() -> None:
    """``decide_completion`` reports tokens exhaustion at end-of-round 0; the
    hook + SSE fire from inside the for-loop's break branch."""
    router, _ = _make_router()
    provider = ScriptedProvider(
        rounds=[
            [
                _tc("c1", "echo", {"text": "x"}),
                _finish(
                    "tool_calls",
                    usage=LLMUsage(prompt_tokens=900, completion_tokens=200, total_tokens=1_100),
                ),
            ],
        ]
    )
    loop = ChatLoop(
        _make_config(
            provider,
            router,
            budget=BudgetLimit(max_tokens=100, max_steps=10, max_tool_calls=10),
        )
    )

    hook_calls: list[BudgetExhaustedCtx] = []

    async def cb(ctx: BudgetExhaustedCtx) -> None:
        hook_calls.append(ctx)

    loop.on("budget_exhausted", cb)
    events = await _collect(loop)

    assert [c.limit_kind for c in hook_calls] == ["tokens"]
    assert hook_calls[0].budget_state["tokens_used"] == 1_100

    be_events = [e for e in events if e.event == "budget_exhausted"]
    assert len(be_events) == 1
    assert be_events[0].payload["limitKind"] == "tokens"


@pytest.mark.asyncio
async def test_budget_exhausted_hook_on_tool_calls_path() -> None:
    router, _ = _make_router()
    provider = ScriptedProvider(
        rounds=[
            [
                _tc("a", "echo", {"text": "x"}),
                _tc("b", "echo", {"text": "y"}),
                _finish("tool_calls"),
            ],
        ]
    )
    loop = ChatLoop(
        _make_config(
            provider,
            router,
            budget=BudgetLimit(max_tokens=1_000_000, max_steps=10, max_tool_calls=1),
        )
    )

    hook_calls: list[BudgetExhaustedCtx] = []

    async def cb(ctx: BudgetExhaustedCtx) -> None:
        hook_calls.append(ctx)

    loop.on("budget_exhausted", cb)
    events = await _collect(loop)

    assert [c.limit_kind for c in hook_calls] == ["tool_calls"]
    assert hook_calls[0].budget_state["tool_calls_used"] == 2

    be_events = [e for e in events if e.event == "budget_exhausted"]
    assert len(be_events) == 1
    assert be_events[0].payload["limitKind"] == "tool_calls"


@pytest.mark.asyncio
async def test_budget_exhausted_hook_on_rounds_path() -> None:
    """``max_rounds`` is the only cap owned by ChatLoop itself; it fires via
    the for-else clause and reports ``limit_kind="rounds"``."""
    router, _ = _make_router()
    provider = ScriptedProvider(
        rounds=[
            [_tc(f"c{i}", "echo", {"text": "x"}), _finish("tool_calls")]
            for i in range(5)
        ]
    )
    loop = ChatLoop(_make_config(provider, router, budget=None, max_rounds=3))

    hook_calls: list[BudgetExhaustedCtx] = []

    async def cb(ctx: BudgetExhaustedCtx) -> None:
        hook_calls.append(ctx)

    loop.on("budget_exhausted", cb)
    events = await _collect(loop)

    assert [c.limit_kind for c in hook_calls] == ["rounds"]
    be_events = [e for e in events if e.event == "budget_exhausted"]
    assert len(be_events) == 1
    assert be_events[0].payload["limitKind"] == "rounds"


@pytest.mark.asyncio
async def test_budget_exhausted_hook_fires_at_most_once() -> None:
    """The four exhaustion paths are mutually exclusive — exactly one fires
    per run. Cross-checks ``budget_exhaust_handled``'s correctness."""
    router, _ = _make_router()
    # Both tokens (1100 > 100) and tool_calls (1 > 0) would trip; tokens wins
    # because decide_completion checks them in order. Either way only one
    # hook firing is expected.
    provider = ScriptedProvider(
        rounds=[
            [
                _tc("c1", "echo", {"text": "x"}),
                _finish(
                    "tool_calls",
                    usage=LLMUsage(prompt_tokens=900, completion_tokens=200, total_tokens=1_100),
                ),
            ],
        ]
    )
    loop = ChatLoop(
        _make_config(
            provider,
            router,
            budget=BudgetLimit(max_tokens=100, max_steps=100, max_tool_calls=0),
        )
    )

    hook_calls: list[BudgetExhaustedCtx] = []

    async def cb(ctx: BudgetExhaustedCtx) -> None:
        hook_calls.append(ctx)

    loop.on("budget_exhausted", cb)
    await _collect(loop)
    assert len(hook_calls) == 1


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class _BlockingProvider:
    """``stream`` awaits an event that the test never sets — used to wedge
    the loop in the middle of a round so a cancellation can hit it."""

    name = "blocking"
    model = "blocking-model"

    def __init__(self, started: asyncio.Event) -> None:
        self._started = started

    async def complete(self, *a: Any, **kw: Any) -> tuple[LLMMessage, Any]:
        raise NotImplementedError

    async def stream(self, messages: Sequence[LLMMessage], **kwargs: Any) -> AsyncIterator[LLMStreamChunk]:
        self._started.set()
        # Block forever until cancelled.
        await asyncio.Event().wait()
        return
        yield LLMStreamChunk()  # type: ignore[unreachable]


@pytest.mark.asyncio
async def test_cancellation_fires_loop_end_with_status_cancelled() -> None:
    router, _ = _make_router()
    started = asyncio.Event()
    provider = _BlockingProvider(started)
    loop = ChatLoop(_make_config(provider, router))

    ends: list[LoopEndCtx] = []

    async def on_end(ctx: LoopEndCtx) -> None:
        ends.append(ctx)

    loop.on("loop_end", on_end)

    collected: list[SSEEvent] = []

    async def consume() -> None:
        async for ev in loop.run():
            collected.append(ev)

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 1. loop_end fired with cancelled status
    assert len(ends) == 1
    assert ends[0].final_status == "cancelled"
    assert ends[0].final_decision is not None
    assert ends[0].final_decision["status"] == "cancelled"

    # 2. session.start was emitted before cancellation; session.end emitted
    # during cancellation cleanup with finalStatus=cancelled
    events_by_event = [(e.type, e.event) for e in collected]
    assert ("agent", "session.start") in events_by_event
    end_evs = [e for e in collected if e.event == "session.end"]
    assert len(end_evs) == 1
    assert end_evs[0].payload == {"finalStatus": "cancelled"}


@pytest.mark.asyncio
async def test_cancellation_during_tool_dispatch_is_re_raised() -> None:
    """``asyncio.CancelledError`` raised by the tool dispatch propagates out
    cleanly via the outer cancellation handler."""
    router = ToolRouter()
    started = asyncio.Event()

    async def hang() -> ToolResult:
        started.set()
        await asyncio.Event().wait()
        return ToolResult(success=True)  # pragma: no cover

    router.register(hang, description="never returns")
    provider = ScriptedProvider(
        rounds=[[_tc("c1", "hang", {}), _finish("tool_calls")]]
    )
    loop = ChatLoop(_make_config(provider, router))

    ends: list[LoopEndCtx] = []

    async def on_end(ctx: LoopEndCtx) -> None:
        ends.append(ctx)

    loop.on("loop_end", on_end)

    async def consume() -> None:
        async for _ in loop.run():
            pass

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ends == [] or ends[0].final_status == "cancelled"
    # The contract is: at most one loop_end with status=cancelled, never a
    # silent swallow. Either zero (cancelled before fire) or exactly one is
    # acceptable.
    if ends:
        assert ends[0].final_status == "cancelled"
