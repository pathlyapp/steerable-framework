"""W2.3.2: long-session regression — 30+ tool calls, instruction adherence
and peak context occupancy as measurable quantities (not vibes).

The control/experiment shape is the point: the same scripted session runs
with bare hooks and with the W2 stack (observation aging + reminders);
the assertions compare the two, so a regression in either direction
(aging stops working, or starts eating the instruction) fails loud.
"""

from __future__ import annotations

from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall, ToolResult

from steerable_agent_runtime import CoreLoop, LoopConfig, RouterToolExecutor, ToolRouter
from steerable_agent_runtime.hooks import ChainHooks, NoopHooks
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.observation_aging import AgingRules, ObservationAgingHooks
from steerable_agent_runtime.reminders import ReminderHooks
from steerable_agent_runtime.tokens import estimate_tokens

from test_loop import make_provider, tc

#: Distinctive instruction the scripted provider checks for in every request.
_SYSTEM = "You are a careful operator. Always end with a plain-language summary."
_MARKER = "careful operator"
#: Each fake read returns ~this many chars — big enough for aging to matter.
_PAYLOAD = "row of data\n" * 800  # ~8 KB ≈ 2k tokens per tool result
_ROUNDS = 35


def _script(rounds: int) -> list[dict[str, Any]]:
    script = [
        {
            "content": "",
            "tool_calls": [tc("read_file", {"path": f"f{i}.txt"}, call_id=f"c{i}")],
        }
        for i in range(rounds)
    ]
    script.append({"content": "All files read. Summary: nothing unusual."})
    return script


def _router() -> ToolRouter:
    router = ToolRouter()

    def read_file(path: str) -> dict:
        """Read a file."""
        return {"path": path, "content": _PAYLOAD}

    router.register(read_file, mode="read")
    return router


async def _run_session(hooks: Any) -> tuple[Any, list[Any]]:
    provider = make_provider(_script(_ROUNDS))
    loop = CoreLoop(
        provider,
        RouterToolExecutor(_router(), consent_granted=True),
        config=LoopConfig(max_rounds=_ROUNDS + 10, max_tool_errors=16, tool_dedup=False),
        hooks=hooks,
    )
    events = [
        e
        async for e in loop.run(
            [
                LLMMessage.text_of("system", _SYSTEM),
                LLMMessage.text_of("user", "survey the files"),
            ]
        )
    ]
    return provider, events


def _peak_tokens(provider: Any) -> int:
    return max(estimate_tokens(call) for call in provider.calls)


@pytest.mark.asyncio
async def test_long_session_aging_halves_peak_context() -> None:
    control_provider, control_events = await _run_session(NoopHooks())
    aged_provider, aged_events = await _run_session(
        ChainHooks(
            ObservationAgingHooks(
                AgingRules(
                    fresh_rounds=3, keep_tokens=50, fold_after_rounds=8,
                    compress_tokens=500,
                )
            ),
            ReminderHooks(max_tool_errors=16),
        )
    )

    # Both sessions complete with a final answer (the session works either way).
    for events in (control_events, aged_events):
        completions = [e for e in events if e.kind == "completion"]
        final = completions[-1].data
        assert final.get("finishReason") == "stop" and final.get("textLength", 0) > 0

    control_peak = _peak_tokens(control_provider)
    aged_peak = _peak_tokens(aged_provider)
    # Aging must cut the peak materially — 35 × ~2k-token results otherwise
    # accumulate without bound. A weak effect (<40% reduction) means the
    # tiers regressed.
    assert aged_peak < control_peak * 0.6, (
        f"aging peak {aged_peak} vs control {control_peak}: effect too weak"
    )


@pytest.mark.asyncio
async def test_long_session_instruction_survives_aging() -> None:
    """Instruction adherence, structural: every request — including the
    last, after 35 tool calls and multiple aging rewrites — still opens
    with the system instruction."""
    provider, _ = await _run_session(
        ChainHooks(ObservationAgingHooks(AgingRules(keep_tokens=50)))
    )
    assert len(provider.calls) >= _ROUNDS
    for i, call in enumerate(provider.calls):
        assert call[0].role == "system", f"request {i} lost its system message"
        assert _MARKER in call[0].content_text, f"request {i} instruction degraded"


@pytest.mark.asyncio
async def test_long_session_old_results_folded_in_late_requests() -> None:
    # keep_tokens sits below the compressed size (~40 tokens) so the fold
    # tier is reachable — a result compressed under keep_tokens is cheap
    # enough to keep verbatim forever by design.
    provider, _ = await _run_session(
        ChainHooks(
            ObservationAgingHooks(
                AgingRules(keep_tokens=10, fold_after_rounds=8, compress_tokens=500)
            )
        )
    )
    late = provider.calls[-1]
    folded = [
        m for m in late
        if m.role == "tool" and "observation folded" in m.content_text
    ]
    assert folded, "late requests should carry folded stubs for early results"
    # The fold stub names where the full record lives (W2.1.4).
    assert "session history" in folded[0].content_text


@pytest.mark.asyncio
async def test_long_session_runaway_reminder_fires() -> None:
    """35 reads without a write is the runaway-exploration failure
    mode; the reminder must land at the highest-recency position."""
    provider, _ = await _run_session(
        ChainHooks(ReminderHooks(max_tool_errors=16, rules=None))
    )
    hits = [
        call
        for call in provider.calls
        if any("tool calls since the last write" in m.content_text for m in call)
    ]
    assert hits, "runaway reminder never fired"
    # Recency: in the firing request the reminder is the LAST message.
    firing = hits[0]
    assert "tool calls since the last write" in firing[-1].content_text
