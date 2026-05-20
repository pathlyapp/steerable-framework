"""A1.3 budget + completion-decision tests for ``ChatLoop``.

Covers:
* ``LoopConfig.budget=None`` → loop ignores budget, behaves like A1.2
* tokens / steps / tool_calls budget dimensions each trip
  ``final_status="budget_exhausted"`` with the correct ``limit_kind``
* ``max_rounds`` (the only ChatLoop-owned cap) trips with
  ``limit_kind="rounds"``
* ``decide_completion`` populates ``RoundEndCtx.decision`` every round
* a terminal-success tool result ends the loop with ``status="completed"``
* a terminal-failure tool result ends the loop with ``status="failed"``
* aggregated ``LLMUsage`` carries through even when the loop exits via
  budget exhaustion (we don't want to throw away the token spend)
* the natural-stop path (no tool calls) is reported as
  ``status=completed, reason=no_tool_calls``
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from typing import Any

import pytest

from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime import (
    BudgetLimit,
    ChatLoop,
    LLMMessage,
    LLMStreamChunk,
    LLMUsage,
    LoopConfig,
    LoopEndCtx,
    RoundEndCtx,
    ToolRouter,
)


# ---------------------------------------------------------------------------
# Test doubles (compact copy of the ones in test_chat_loop_round.py so this
# file is self-contained — the scripted-provider pattern is small enough that
# duplication beats a shared fixture in test readability)
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


def _text(text: str) -> LLMStreamChunk:
    return LLMStreamChunk(content_delta=text)


def _tc(id: str, name: str, args: dict[str, Any]) -> LLMStreamChunk:
    return LLMStreamChunk(tool_call_delta=ToolCall(id=id, name=name, arguments=args))


def _finish(reason: str = "stop", usage: LLMUsage | None = None) -> LLMStreamChunk:
    return LLMStreamChunk(finish_reason=reason, usage=usage)


def _make_router_with_tools() -> tuple[ToolRouter, dict[str, Any]]:
    obs: dict[str, Any] = {"calls": []}
    router = ToolRouter()

    async def echo(text: str = "") -> dict[str, Any]:
        obs["calls"].append(("echo", text))
        return {"echoed": text}

    async def stop_here() -> ToolResult:
        """Returns a terminal-success result, ends the loop with status=completed."""
        obs["calls"].append(("stop_here",))
        return ToolResult(success=True, terminal=True, message="all done")

    async def die_here() -> ToolResult:
        """Returns a terminal-failure result, ends the loop with status=failed."""
        obs["calls"].append(("die_here",))
        return ToolResult(success=False, terminal=True, error="catastrophe")

    router.register(echo, description="Echo")
    router.register(stop_here, description="Returns terminal-success")
    router.register(die_here, description="Returns terminal-failure")
    return router, obs


def _make_config(
    provider: ScriptedProvider,
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


# ---------------------------------------------------------------------------
# budget=None → loop ignores budget, behaves like A1.2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_budget_means_only_max_rounds_caps_the_loop() -> None:
    router, _ = _make_router_with_tools()
    provider = ScriptedProvider(rounds=[[_finish("stop")]])
    loop = ChatLoop(_make_config(provider, router, budget=None, max_rounds=12))

    captured: dict[str, Any] = {}

    async def on_end(ctx: LoopEndCtx) -> None:
        captured["status"] = ctx.final_status
        captured["decision"] = ctx.final_decision

    loop.on("loop_end", on_end)

    async for _ in loop.run():
        pass

    assert captured["status"] == "completed"
    assert captured["decision"]["reason"] == "no_tool_calls"


# ---------------------------------------------------------------------------
# Budget: tokens dimension
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tokens_budget_exhaustion_after_first_round() -> None:
    """Round 0 reports 5_000 tokens against a max_tokens=100 cap →
    decide_completion reports budget_exhausted at the end of round 0.
    """
    router, _ = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            [
                _tc("c1", "echo", {"text": "hi"}),
                _finish(
                    "tool_calls",
                    usage=LLMUsage(prompt_tokens=4_000, completion_tokens=1_000, total_tokens=5_000),
                ),
            ],
            # If the loop misbehaves and continues, this round would succeed —
            # the test relies on the budget cap to never reach it.
            [_finish("stop")],
        ]
    )
    loop = ChatLoop(
        _make_config(
            provider,
            router,
            budget=BudgetLimit(max_tokens=100, max_steps=10, max_tool_calls=10),
        )
    )

    seen: dict[str, Any] = {}

    async def on_end(ctx: LoopEndCtx) -> None:
        seen["status"] = ctx.final_status
        seen["decision"] = ctx.final_decision
        seen["usage"] = ctx.total_usage

    loop.on("loop_end", on_end)

    async for _ in loop.run():
        pass

    assert seen["status"] == "budget_exhausted"
    assert seen["decision"]["limit_kind"] == "tokens"
    assert "max_tokens=100" in seen["decision"]["reason"]
    # Loop must not have spent a second LLM call after exhaustion.
    assert len(provider.calls) == 1
    # And we still report the tokens that were actually consumed.
    assert seen["usage"]["total_tokens"] == 5_000


# ---------------------------------------------------------------------------
# Budget: steps dimension (pre-debit at round entry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steps_budget_exhaustion_at_round_entry() -> None:
    """``max_steps=1`` lets exactly one round run; round 1's entry-time
    ``consume_budget(step=True)`` reports exhausted and the loop bails out
    before the second LLM call."""
    router, _ = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            [
                _tc("c1", "echo", {"text": "first"}),
                _finish("tool_calls"),
            ],
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

    seen: dict[str, Any] = {}

    async def on_end(ctx: LoopEndCtx) -> None:
        seen["status"] = ctx.final_status
        seen["decision"] = ctx.final_decision

    loop.on("loop_end", on_end)

    async for _ in loop.run():
        pass

    assert seen["status"] == "budget_exhausted"
    assert seen["decision"]["limit_kind"] == "steps"
    # Loop pre-debited round 1's step, hit the cap, and broke before the
    # second provider call.
    assert len(provider.calls) == 1


# ---------------------------------------------------------------------------
# Budget: tool_calls dimension
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_calls_budget_exhaustion_after_first_round() -> None:
    """Two tool calls in round 0, ``max_tool_calls=1`` → at end of round 0
    decide_completion reports tool_calls exhaustion."""
    router, _ = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            [
                _tc("a", "echo", {"text": "x"}),
                _tc("b", "echo", {"text": "y"}),
                _finish("tool_calls"),
            ],
            [_finish("stop")],
        ]
    )
    loop = ChatLoop(
        _make_config(
            provider,
            router,
            budget=BudgetLimit(max_tokens=1_000_000, max_steps=10, max_tool_calls=1),
        )
    )

    seen: dict[str, Any] = {}

    async def on_end(ctx: LoopEndCtx) -> None:
        seen["status"] = ctx.final_status
        seen["decision"] = ctx.final_decision

    loop.on("loop_end", on_end)

    async for _ in loop.run():
        pass

    assert seen["status"] == "budget_exhausted"
    assert seen["decision"]["limit_kind"] == "tool_calls"
    # Two calls already executed before the decision fired — the cap is an
    # end-of-round check, not a per-call abort.
    assert len(provider.calls) == 1


# ---------------------------------------------------------------------------
# max_rounds (ChatLoop-owned cap)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_rounds_caps_loop_with_limit_kind_rounds() -> None:
    """An infinite tool-call loop must hit ``max_rounds`` and exit with
    ``limit_kind="rounds"`` — distinct from harness budget dimensions."""
    router, _ = _make_router_with_tools()
    rounds = [
        [_tc(f"c{i}", "echo", {"text": "x"}), _finish("tool_calls")]
        for i in range(10)
    ]
    provider = ScriptedProvider(rounds=rounds)
    loop = ChatLoop(_make_config(provider, router, budget=None, max_rounds=3))

    seen: dict[str, Any] = {}

    async def on_end(ctx: LoopEndCtx) -> None:
        seen["status"] = ctx.final_status
        seen["decision"] = ctx.final_decision
        seen["rounds"] = ctx.rounds_completed

    loop.on("loop_end", on_end)

    async for _ in loop.run():
        pass

    assert seen["status"] == "budget_exhausted"
    assert seen["decision"]["limit_kind"] == "rounds"
    assert seen["rounds"] == 3


# ---------------------------------------------------------------------------
# Terminal results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_success_ends_loop_with_status_completed() -> None:
    router, obs = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            [
                _tc("c1", "stop_here", {}),
                _finish("tool_calls"),
            ],
            # The loop must not reach this round; verifies via call count.
            [_text("after"), _finish("stop")],
        ]
    )
    loop = ChatLoop(_make_config(provider, router))

    seen: dict[str, Any] = {}

    async def on_end(ctx: LoopEndCtx) -> None:
        seen["status"] = ctx.final_status
        seen["decision"] = ctx.final_decision

    loop.on("loop_end", on_end)

    async for _ in loop.run():
        pass

    assert obs["calls"] == [("stop_here",)]
    assert seen["status"] == "completed"
    assert seen["decision"]["reason"] == "terminal_result"
    assert seen["decision"]["terminal_index"] == 0
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_terminal_failure_ends_loop_with_status_failed() -> None:
    router, obs = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            [
                _tc("c1", "die_here", {}),
                _finish("tool_calls"),
            ],
        ]
    )
    loop = ChatLoop(_make_config(provider, router))

    seen: dict[str, Any] = {}

    async def on_end(ctx: LoopEndCtx) -> None:
        seen["status"] = ctx.final_status
        seen["decision"] = ctx.final_decision

    loop.on("loop_end", on_end)

    async for _ in loop.run():
        pass

    assert obs["calls"] == [("die_here",)]
    assert seen["status"] == "failed"
    assert seen["decision"]["reason"] == "terminal_failure"
    assert seen["decision"]["terminal_index"] == 0


# ---------------------------------------------------------------------------
# RoundEndCtx.decision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_end_ctx_decision_populated_every_round() -> None:
    """Every after_round fire carries a decision dict. While executing,
    status is ``"executing"`` with ``reason="has_pending_tool_calls"``."""
    router, _ = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            [
                _tc("c1", "echo", {"text": "x"}),
                _finish("tool_calls"),
            ],
            [_finish("stop")],
        ]
    )
    loop = ChatLoop(_make_config(provider, router))

    decisions: list[dict[str, Any]] = []

    async def cb(ctx: RoundEndCtx) -> None:
        assert ctx.decision is not None, "decision must be populated by A1.3"
        decisions.append(ctx.decision)

    loop.on("after_round", cb)

    async for _ in loop.run():
        pass

    # Round 0: executing (echo result is not terminal, more tool_calls coming).
    # Round 1: completed (no tool_calls).
    assert [d["status"] for d in decisions] == ["executing", "completed"]
    assert decisions[0]["reason"] == "has_pending_tool_calls"
    assert decisions[1]["reason"] == "no_tool_calls"


# ---------------------------------------------------------------------------
# Aggregated usage still reported on exhaustion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_total_usage_reported_even_on_budget_exhaustion() -> None:
    """The spent tokens must reach ``LoopEndCtx.total_usage`` regardless of
    why the loop ended — we never silently lose accounting."""
    router, _ = _make_router_with_tools()
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
            budget=BudgetLimit(max_tokens=1_000, max_steps=10, max_tool_calls=10),
        )
    )

    seen: dict[str, Any] = {}

    async def on_end(ctx: LoopEndCtx) -> None:
        seen["status"] = ctx.final_status
        seen["usage"] = ctx.total_usage

    loop.on("loop_end", on_end)

    async for _ in loop.run():
        pass

    assert seen["status"] == "budget_exhausted"
    assert seen["usage"] == {"prompt_tokens": 900, "completion_tokens": 200, "total_tokens": 1_100}


# ---------------------------------------------------------------------------
# Budget priority over completion verdict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_priority_over_terminal_result() -> None:
    """If the LLM stream reports usage that exhausts the token budget AND
    the tool reports terminal success in the same round, the loop must
    report ``budget_exhausted`` (the spend overrun is the more important
    signal — happy stops can mask runaway cost)."""
    router, _ = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            [
                _tc("c1", "stop_here", {}),
                _finish(
                    "tool_calls",
                    usage=LLMUsage(prompt_tokens=500, completion_tokens=200, total_tokens=700),
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

    seen: dict[str, Any] = {}

    async def on_end(ctx: LoopEndCtx) -> None:
        seen["status"] = ctx.final_status
        seen["decision"] = ctx.final_decision

    loop.on("loop_end", on_end)

    async for _ in loop.run():
        pass

    assert seen["status"] == "budget_exhausted"
    assert seen["decision"]["limit_kind"] == "tokens"
