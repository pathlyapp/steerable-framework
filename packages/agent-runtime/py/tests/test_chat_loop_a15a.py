"""A1.5a — loop-owned safety nets.

Covers two ChatLoop-internal caps that were promised by ``LoopConfig`` from
A1.1 but only landed for real in A1.5a:

* ``max_tool_result_bytes`` — oversized tool results are wrapped in a
  ``{"truncated": True, "preview": ..., "original_bytes": N}`` envelope
  before being handed to the LLM; the original ``ToolResult`` (and the
  ``after_tool_result`` ctx) are unchanged.
* ``max_elapsed_seconds`` — wall-clock guard checked at each round entry.
  Tripping fires ``budget_exhausted`` with ``limit_kind="time"`` and emits
  a matching SSE event.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterable, Sequence
from typing import Any

import pytest

from steerable_agent_protocol.generated import SSEEvent, ToolCall, ToolResult
from steerable_agent_runtime import (
    BudgetExhaustedCtx,
    ChatLoop,
    LLMMessage,
    LLMStreamChunk,
    LLMUsage,
    LoopConfig,
    LoopEndCtx,
    ToolResultCtx,
    ToolRouter,
)
from steerable_agent_runtime.chat_loop import _truncate_oversized


# ---------------------------------------------------------------------------
# Test doubles
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


class _SlowProvider:
    """``stream`` sleeps for ``delay_seconds`` *between* rounds to let the
    wall-clock cap trip without blocking inside a single stream forever."""

    name = "slow"
    model = "slow-model"

    def __init__(self, rounds: list[list[LLMStreamChunk]], delay_seconds: float) -> None:
        self._rounds = rounds
        self._delay = delay_seconds
        self._next = 0

    async def complete(self, *a: Any, **kw: Any) -> tuple[LLMMessage, Any]:
        raise NotImplementedError

    async def stream(
        self,
        messages: Sequence[LLMMessage],
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        await asyncio.sleep(self._delay)
        idx = self._next
        self._next += 1
        if idx >= len(self._rounds):
            return
        for chunk in self._rounds[idx]:
            yield chunk


def _text(text: str) -> LLMStreamChunk:
    return LLMStreamChunk(content_delta=text)


def _tc(id: str, name: str, args: dict[str, Any]) -> LLMStreamChunk:
    return LLMStreamChunk(tool_call_delta=ToolCall(id=id, name=name, arguments=args))


def _finish(reason: str = "stop", usage: LLMUsage | None = None) -> LLMStreamChunk:
    return LLMStreamChunk(finish_reason=reason, usage=usage)


def _make_config(
    provider: Any,
    router: ToolRouter,
    *,
    max_rounds: int = 12,
    max_elapsed_seconds: float = 180.0,
    max_tool_result_bytes: int = 64 * 1024,
) -> LoopConfig:
    return LoopConfig(
        provider=provider,
        provider_kind="openai_compat",
        tool_router=router,
        initial_messages=[LLMMessage(role="user", content="go")],
        max_rounds=max_rounds,
        max_elapsed_seconds=max_elapsed_seconds,
        max_tool_result_bytes=max_tool_result_bytes,
    )


async def _collect(loop: ChatLoop) -> list[SSEEvent]:
    return [ev async for ev in loop.run()]


# ---------------------------------------------------------------------------
# _truncate_oversized — pure-function tests
# ---------------------------------------------------------------------------


class TestTruncateOversized:
    def test_returns_input_unchanged_when_within_cap(self) -> None:
        assert _truncate_oversized("hello", 100) == "hello"

    def test_returns_input_unchanged_when_exactly_at_cap(self) -> None:
        assert _truncate_oversized("x" * 100, 100) == "x" * 100

    def test_wraps_oversized_in_truncated_envelope(self) -> None:
        oversized = "abcdef" * 1_000  # 6_000 bytes
        out = _truncate_oversized(oversized, 100)
        parsed = json.loads(out)
        assert parsed["truncated"] is True
        assert parsed["original_bytes"] == 6_000
        # preview keeps ~70% of the budget; the JSON envelope is small enough
        # that final output remains close to (not over) max_bytes-ish.
        assert "preview" in parsed
        assert isinstance(parsed["preview"], str)
        assert parsed["preview"].startswith("abc")

    def test_zero_or_negative_cap_disables_truncation(self) -> None:
        massive = "x" * 100_000
        assert _truncate_oversized(massive, 0) == massive
        assert _truncate_oversized(massive, -1) == massive

    def test_preview_decoded_at_utf8_boundary(self) -> None:
        """The cut at ``preview_bytes`` must not produce invalid UTF-8 — a
        Chinese character is 3 bytes, so a sloppy cut between 1 and 3 of
        its bytes would otherwise yield ``UnicodeDecodeError``.
        """
        # "中" is 0xe4 0xb8 0xad (3 bytes). 30 copies = 90 bytes. Force
        # max_bytes=50 so preview_bytes ≈ 35, definitely mid-codepoint.
        massive = "中" * 30
        out = _truncate_oversized(massive, 50)
        parsed = json.loads(out)
        # Preview is a valid string (decoded with errors='ignore' so it
        # stops at the last clean codepoint boundary).
        assert isinstance(parsed["preview"], str)
        assert all(ch == "中" for ch in parsed["preview"])


# ---------------------------------------------------------------------------
# max_tool_result_bytes — integration with ChatLoop
# ---------------------------------------------------------------------------


def _router_with_huge_echo() -> ToolRouter:
    router = ToolRouter()

    async def huge_echo(text: str = "") -> ToolResult:
        # Caller controls payload size via the ``text`` argument.
        return ToolResult(
            success=True,
            data={"value": text},
            message=None,  # force serialise_tool_result to dump the dict
        )

    router.register(huge_echo, description="Echoes a payload")
    return router


@pytest.mark.asyncio
async def test_oversized_tool_result_is_truncated_in_llm_message() -> None:
    """The second round's ``messages`` (as seen by the provider) carries the
    truncated envelope, not the raw payload."""
    router = _router_with_huge_echo()
    big = "x" * 20_000
    provider = ScriptedProvider(
        rounds=[
            [_tc("c1", "huge_echo", {"text": big}), _finish("tool_calls")],
            [_finish("stop")],
        ]
    )
    loop = ChatLoop(_make_config(provider, router, max_tool_result_bytes=1_000))

    await _collect(loop)

    # Round 1 receives the (now-truncated) tool message in its messages list.
    assert len(provider.calls) == 2
    round1_msgs = provider.calls[1]["messages"]
    tool_msgs = [m for m in round1_msgs if m.role == "tool"]
    assert len(tool_msgs) == 1
    content = tool_msgs[0].content
    assert content is not None
    parsed = json.loads(content)
    assert parsed["truncated"] is True
    assert parsed["original_bytes"] > 1_000
    # original payload no longer reaches the model verbatim
    assert big not in content


@pytest.mark.asyncio
async def test_after_tool_result_hook_sees_untruncated_result() -> None:
    """Truncation applies only to the LLM-visible string; the hook ctx still
    carries the full original ``ToolResult``."""
    router = _router_with_huge_echo()
    big = "x" * 20_000
    provider = ScriptedProvider(
        rounds=[
            [_tc("c1", "huge_echo", {"text": big}), _finish("tool_calls")],
            [_finish("stop")],
        ]
    )
    loop = ChatLoop(_make_config(provider, router, max_tool_result_bytes=1_000))

    captured: list[ToolResult] = []

    async def cb(ctx: ToolResultCtx) -> None:
        captured.append(ctx.tool_result)

    loop.on("after_tool_result", cb)
    await _collect(loop)

    assert len(captured) == 1
    # ``ToolRouter.dispatch`` injects a ``durationMs`` key but the user
    # payload survives intact — that's what "untruncated" means here.
    assert captured[0].data is not None
    assert captured[0].data["value"] == big


@pytest.mark.asyncio
async def test_within_cap_tool_result_passes_through_verbatim() -> None:
    """A small payload should be re-fed unchanged — no envelope, no encoding."""
    router = _router_with_huge_echo()
    payload = "hello"
    provider = ScriptedProvider(
        rounds=[
            [_tc("c1", "huge_echo", {"text": payload}), _finish("tool_calls")],
            [_finish("stop")],
        ]
    )
    loop = ChatLoop(_make_config(provider, router, max_tool_result_bytes=10_000))

    await _collect(loop)

    round1_msgs = provider.calls[1]["messages"]
    tool_msg = next(m for m in round1_msgs if m.role == "tool")
    assert tool_msg.content is not None
    # Round-trip: still parses, no truncated marker.
    parsed = json.loads(tool_msg.content)
    assert "truncated" not in parsed
    assert parsed["data"]["value"] == payload


@pytest.mark.asyncio
async def test_max_tool_result_bytes_zero_disables_truncation() -> None:
    router = _router_with_huge_echo()
    big = "x" * 20_000
    provider = ScriptedProvider(
        rounds=[
            [_tc("c1", "huge_echo", {"text": big}), _finish("tool_calls")],
            [_finish("stop")],
        ]
    )
    loop = ChatLoop(_make_config(provider, router, max_tool_result_bytes=0))

    await _collect(loop)
    tool_msg = next(m for m in provider.calls[1]["messages"] if m.role == "tool")
    assert tool_msg.content is not None
    assert big in tool_msg.content  # full payload passed through


# ---------------------------------------------------------------------------
# max_elapsed_seconds — wall-clock guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_elapsed_guard_trips_with_limit_kind_time() -> None:
    """A provider that sleeps 200ms per round + ``max_elapsed_seconds=0.1`` →
    round 1's entry detects the overrun and trips ``budget_exhausted`` with
    ``limit_kind="time"``. Round 0 already completed because the guard
    fires at *round entry*, not mid-stream."""
    router = ToolRouter()

    async def echo(text: str = "") -> dict[str, Any]:
        return {"echoed": text}

    router.register(echo, description="Echo")

    provider = _SlowProvider(
        rounds=[
            [_tc("c1", "echo", {"text": "first"}), _finish("tool_calls")],
            [_tc("c2", "echo", {"text": "second"}), _finish("tool_calls")],
            [_finish("stop")],
        ],
        delay_seconds=0.2,
    )
    loop = ChatLoop(_make_config(provider, router, max_elapsed_seconds=0.1))

    hook_calls: list[BudgetExhaustedCtx] = []
    end_calls: list[LoopEndCtx] = []

    async def on_budget(ctx: BudgetExhaustedCtx) -> None:
        hook_calls.append(ctx)

    async def on_end(ctx: LoopEndCtx) -> None:
        end_calls.append(ctx)

    loop.on("budget_exhausted", on_budget)
    loop.on("loop_end", on_end)

    events = await _collect(loop)

    assert len(hook_calls) == 1
    assert hook_calls[0].limit_kind == "time"

    be_events = [e for e in events if e.event == "budget_exhausted"]
    assert len(be_events) == 1
    assert be_events[0].payload is not None
    assert be_events[0].payload["limitKind"] == "time"

    assert len(end_calls) == 1
    assert end_calls[0].final_status == "budget_exhausted"
    assert end_calls[0].final_decision is not None
    assert end_calls[0].final_decision["limit_kind"] == "time"
    assert end_calls[0].final_decision["reason"].startswith("elapsed=")


@pytest.mark.asyncio
async def test_elapsed_guard_does_not_interrupt_inflight_round() -> None:
    """A round that started before the cap completes its LLM stream + tool
    dispatch even if the cap is breached mid-round. The guard catches it on
    the *next* round entry."""
    router = ToolRouter()
    dispatched: list[str] = []

    async def echo(text: str = "") -> dict[str, Any]:
        dispatched.append(text)
        return {"echoed": text}

    router.register(echo, description="Echo")

    # Round 0 takes 200ms to stream, but the tool dispatch is immediate.
    # Cap is 100ms, so round 0 should still complete (tool dispatched),
    # then round 1's entry trips the guard.
    provider = _SlowProvider(
        rounds=[
            [_tc("c1", "echo", {"text": "did-run"}), _finish("tool_calls")],
            [_finish("stop")],  # would-run-but-guard-trips
        ],
        delay_seconds=0.2,
    )
    loop = ChatLoop(_make_config(provider, router, max_elapsed_seconds=0.1))

    await _collect(loop)

    # Round 0's tool was dispatched (proving the round completed).
    assert dispatched == ["did-run"]
    # But round 1's LLM call was never made.
    # _SlowProvider counter starts at 0, increments after each stream call.
    assert provider._next == 1  # only round 0 ran


@pytest.mark.asyncio
async def test_elapsed_guard_disabled_when_zero_or_negative() -> None:
    """``max_elapsed_seconds <= 0`` means "no cap" by convention; the loop
    must not trip ``budget_exhausted`` on wall-clock alone."""
    router = ToolRouter()

    async def echo(text: str = "") -> dict[str, Any]:
        return {"echoed": text}

    router.register(echo, description="Echo")

    # Use _SlowProvider so wall-clock would otherwise be triggered.
    provider = _SlowProvider(
        rounds=[[_finish("stop")]],
        delay_seconds=0.05,
    )
    loop = ChatLoop(_make_config(provider, router, max_elapsed_seconds=0))

    hook_calls: list[BudgetExhaustedCtx] = []

    async def on_budget(ctx: BudgetExhaustedCtx) -> None:
        hook_calls.append(ctx)

    loop.on("budget_exhausted", on_budget)
    events = await _collect(loop)

    # No budget_exhausted fired — natural completion via "stop".
    assert hook_calls == []
    assert not any(e.event == "budget_exhausted" for e in events)


@pytest.mark.asyncio
async def test_elapsed_guard_does_not_fire_when_loop_completes_quickly() -> None:
    """Sanity: a fast natural-stop run with a generous cap must complete
    normally without ever consulting the wall-clock path."""
    router = ToolRouter()
    provider = ScriptedProvider(rounds=[[_finish("stop")]])
    loop = ChatLoop(_make_config(provider, router, max_elapsed_seconds=60.0))

    hook_calls: list[BudgetExhaustedCtx] = []

    async def on_budget(ctx: BudgetExhaustedCtx) -> None:
        hook_calls.append(ctx)

    loop.on("budget_exhausted", on_budget)
    await _collect(loop)
    assert hook_calls == []


# ---------------------------------------------------------------------------
# Cross-interaction: time vs. rounds vs. budget — priority sanity check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_time_guard_can_fire_before_max_rounds() -> None:
    """When wall-clock is the binding cap (smaller than max_rounds would
    allow), the loop reports ``limit_kind="time"``, not ``"rounds"``."""
    router = ToolRouter()

    async def echo(text: str = "") -> dict[str, Any]:
        return {"echoed": text}

    router.register(echo, description="Echo")

    provider = _SlowProvider(
        rounds=[
            [_tc(f"c{i}", "echo", {"text": "x"}), _finish("tool_calls")]
            for i in range(20)
        ],
        delay_seconds=0.15,
    )
    loop = ChatLoop(
        _make_config(provider, router, max_rounds=10, max_elapsed_seconds=0.05)
    )

    hook_calls: list[BudgetExhaustedCtx] = []

    async def on_budget(ctx: BudgetExhaustedCtx) -> None:
        hook_calls.append(ctx)

    loop.on("budget_exhausted", on_budget)
    await _collect(loop)

    assert len(hook_calls) == 1
    assert hook_calls[0].limit_kind == "time"
