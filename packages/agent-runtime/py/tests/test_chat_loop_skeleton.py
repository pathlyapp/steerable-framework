"""Skeleton tests for ``ChatLoop`` — covers the A1.1 public surface.

* construction & hook registration (valid + invalid names, multiple callbacks)
* ``run()`` yields the canonical ``session.start`` / ``done`` / ``session.end``
  envelope
* ``loop_start`` / ``loop_end`` hooks fire with the right ctx
* ``state`` is shared across hooks and isolated across loop instances
* exceptions inside hooks are wrapped as ``HookError``
* returning ``HOOK_SKIP`` from a non-skip-allowed hook raises

Round-body / tool-dispatch tests live in ``test_chat_loop_round.py`` (A1.2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from typing import Any

import pytest

from steerable_agent_protocol.generated import SSEEvent
from steerable_agent_runtime import (
    HOOK_SKIP,
    ChatLoop,
    HookError,
    LLMMessage,
    LLMStreamChunk,
    LoopConfig,
    LoopEndCtx,
    LoopStartCtx,
    ToolRouter,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _EmptyStreamProvider:
    """``LLMProvider`` stub whose ``stream()`` yields nothing.

    A1.2's ``run()`` interprets an empty stream as a natural stop (the
    assistant produced no tool calls), so the loop exits after one round
    without invoking any tools. This keeps the skeleton tests scoped to
    the session envelope + ``loop_start`` / ``loop_end`` hooks.
    """

    name = "fake"
    model = "fake-model"

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> tuple[LLMMessage, Any]:
        raise NotImplementedError("skeleton tests should not call complete()")

    async def stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        # Empty async generator: ``return`` exits the coroutine, the unreachable
        # ``yield`` is what tags the function as an async-generator for typing.
        return
        yield LLMStreamChunk()  # pragma: no cover — never reached


def _make_config(**overrides: Any) -> LoopConfig:
    base: dict[str, Any] = dict(
        provider=_EmptyStreamProvider(),
        provider_kind="openai_compat",
        tool_router=ToolRouter(),
        initial_messages=[LLMMessage(role="user", content="hi")],
        max_rounds=12,
    )
    base.update(overrides)
    return LoopConfig(**base)


# ---------------------------------------------------------------------------
# Construction & registration
# ---------------------------------------------------------------------------


def test_construct_chat_loop() -> None:
    loop = ChatLoop(_make_config())
    assert loop.session_id.startswith("sess_")
    assert loop.trace_id.startswith("tr_")
    assert len(loop.loop_id) == 32  # uuid4 hex


def test_caller_supplied_session_id_is_used() -> None:
    loop = ChatLoop(_make_config(session_id="sess_caller_provided"))
    assert loop.session_id == "sess_caller_provided"


def test_unknown_hook_name_rejected() -> None:
    loop = ChatLoop(_make_config())

    async def cb(ctx: Any) -> None:
        return None

    with pytest.raises(ValueError, match="unknown hook name"):
        loop.on("not_a_real_hook", cb)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# run() — canonical envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_yields_session_envelope() -> None:
    loop = ChatLoop(_make_config())
    events: list[SSEEvent] = [e async for e in loop.run()]

    assert len(events) == 3, f"expected 3 events, got {[e.type for e in events]}"
    # session.start
    assert events[0].type == "agent"
    assert events[0].event == "session.start"
    assert events[0].payload is not None
    assert events[0].payload["sessionId"] == loop.session_id
    assert events[0].payload["traceId"] == loop.trace_id
    # done
    assert events[1].type == "done"
    # session.end
    assert events[2].type == "agent"
    assert events[2].event == "session.end"
    assert events[2].payload is not None
    assert events[2].payload["finalStatus"] == "completed"


# ---------------------------------------------------------------------------
# loop_start / loop_end hooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_start_and_loop_end_hooks_fire() -> None:
    loop = ChatLoop(_make_config())

    calls: list[str] = []

    async def on_start(ctx: LoopStartCtx) -> None:
        calls.append("start")
        assert ctx.session_id == loop.session_id
        assert ctx.trace_id == loop.trace_id
        assert len(ctx.initial_messages) == 1
        assert ctx.initial_messages[0].content == "hi"
        # An empty ToolRouter resolves to an empty descriptor list.
        assert ctx.initial_tools == []

    async def on_end(ctx: LoopEndCtx) -> None:
        calls.append("end")
        assert ctx.final_status == "completed"
        # A1.2: an empty stream is a natural stop after exactly one round.
        assert ctx.rounds_completed == 1

    loop.on("loop_start", on_start)
    loop.on("loop_end", on_end)

    async for _ in loop.run():
        pass

    assert calls == ["start", "end"]


@pytest.mark.asyncio
async def test_multiple_callbacks_per_hook_run_in_order() -> None:
    loop = ChatLoop(_make_config())

    calls: list[int] = []

    async def cb1(ctx: LoopStartCtx) -> None:
        calls.append(1)

    async def cb2(ctx: LoopStartCtx) -> None:
        calls.append(2)

    async def cb3(ctx: LoopStartCtx) -> None:
        calls.append(3)

    loop.on("loop_start", cb1)
    loop.on("loop_start", cb2)
    loop.on("loop_start", cb3)

    async for _ in loop.run():
        pass

    assert calls == [1, 2, 3]


# ---------------------------------------------------------------------------
# Shared state across hooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_is_seeded_from_config() -> None:
    seen: dict[str, Any] = {}

    async def on_start(ctx: LoopStartCtx) -> None:
        seen["counter"] = ctx.state["counter"]
        seen["greeting"] = ctx.state["greeting"]

    loop = ChatLoop(_make_config(initial_state={"counter": 5, "greeting": "hi"}))
    loop.on("loop_start", on_start)

    async for _ in loop.run():
        pass

    assert seen == {"counter": 5, "greeting": "hi"}


@pytest.mark.asyncio
async def test_state_mutations_are_visible_across_hooks() -> None:
    captured: list[int] = []

    async def s1(ctx: LoopStartCtx) -> None:
        ctx.state["counter"] = ctx.state.get("counter", 0) + 1
        captured.append(ctx.state["counter"])

    async def e1(ctx: LoopEndCtx) -> None:
        ctx.state["counter"] = ctx.state.get("counter", 0) + 10
        captured.append(ctx.state["counter"])

    loop = ChatLoop(_make_config(initial_state={"counter": 0}))
    loop.on("loop_start", s1)
    loop.on("loop_end", e1)

    async for _ in loop.run():
        pass

    assert captured == [1, 11]


@pytest.mark.asyncio
async def test_two_loop_instances_have_independent_state() -> None:
    """Mutating one ChatLoop's state must not leak into the next."""

    async def bump(ctx: LoopStartCtx) -> None:
        ctx.state["n"] = ctx.state.get("n", 0) + 1

    cfg = _make_config(initial_state={"n": 0})

    loop1 = ChatLoop(cfg)
    loop1.on("loop_start", bump)
    async for _ in loop1.run():
        pass

    loop2 = ChatLoop(cfg)
    seen: list[int] = []

    async def observe(ctx: LoopStartCtx) -> None:
        seen.append(ctx.state.get("n", 0))

    loop2.on("loop_start", observe)
    async for _ in loop2.run():
        pass

    assert seen == [0], "config.initial_state must not be mutated by ChatLoop"


# ---------------------------------------------------------------------------
# HookError wrapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_exception_wraps_as_HookError() -> None:
    loop = ChatLoop(_make_config())

    async def bad(ctx: LoopStartCtx) -> None:
        raise ValueError("boom")

    loop.on("loop_start", bad)

    with pytest.raises(HookError) as exc_info:
        async for _ in loop.run():
            pass

    assert exc_info.value.name == "loop_start"
    assert isinstance(exc_info.value.cause, ValueError)
    assert "boom" in str(exc_info.value.cause)


# ---------------------------------------------------------------------------
# HOOK_SKIP semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_skip_rejected_for_loop_start() -> None:
    """``loop_start`` is not in _SKIP_ALLOWED; returning HOOK_SKIP must raise."""

    loop = ChatLoop(_make_config())

    async def try_skip(ctx: LoopStartCtx) -> Any:
        return HOOK_SKIP

    loop.on("loop_start", try_skip)

    with pytest.raises(RuntimeError, match="HOOK_SKIP"):
        async for _ in loop.run():
            pass
