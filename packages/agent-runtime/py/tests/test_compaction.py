"""CompactionHooks: context-pressure-driven transcript rewriting (pre_step)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime import (
    CompactionHooks,
    CoreLoop,
    LoopEvent,
    RouterToolExecutor,
    ToolRouter,
    estimate_tokens,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk


def make_provider(script: list[dict[str, Any]]):
    class _FakeProvider:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self.calls: list[list[LLMMessage]] = []
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            self.calls.append(list(messages))
            entry = script[min(self._idx, len(script) - 1)]
            self._idx += 1

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                content = entry.get("content", "")
                if content:
                    yield LLMStreamChunk(content_delta=content)
                for tc in entry.get("tool_calls", []):
                    yield LLMStreamChunk(tool_call_delta=tc)
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop",
                    usage=entry.get("usage"),
                )

            return _gen()

    return _FakeProvider()


def tc(name: str, args: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(id=f"call_{name}", name=name, arguments=args or {})


async def collect(loop_run: AsyncIterator[LoopEvent]) -> list[LoopEvent]:
    return [e async for e in loop_run]


def test_estimate_tokens_scales_with_content() -> None:
    small = [LLMMessage.text_of("user", "hi")]
    big = [LLMMessage.text_of("user", "x" * 40_000)]
    assert estimate_tokens(big) > estimate_tokens(small)


@pytest.mark.asyncio
async def test_under_threshold_transcript_unchanged() -> None:
    provider = make_provider([{"content": "answer"}])
    router = ToolRouter()
    hooks = CompactionHooks(max_context_tokens=60_000, threshold_ratio=0.8)
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)

    await collect(loop.run([LLMMessage.text_of("user", "2+2?")]))
    assert hooks.compactions == 0
    assert provider.calls[0][0].content_text == "2+2?"


@pytest.mark.asyncio
async def test_over_threshold_folds_old_tool_results() -> None:
    # Three tool rounds with big outputs; by round 3 the estimate crosses the
    # threshold and old tool results must be folded into placeholders.
    # (Distinct args per round — identical calls would hit the dedup guard.)
    big = "y" * 3_000
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("emit", {"n": 1})]},
            {"content": "", "tool_calls": [tc("emit", {"n": 2})]},
            {"content": "", "tool_calls": [tc("emit", {"n": 3})]},
            {"content": "final"},
        ]
    )
    router = ToolRouter()

    async def emit(n: int) -> str:
        return big

    router.register(emit)
    hooks = CompactionHooks(
        max_context_tokens=2_000,  # tiny budget to force pressure
        threshold_ratio=0.8,
        keep_last_messages=4,
        keep_last_tool_results=1,
    )
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    assert hooks.compactions >= 1
    # some later model call saw folded tool output instead of the raw blob,
    # keeping a short excerpt as a clue about what the tool returned
    folded = [
        m.content_text
        for call in provider.calls[1:]
        for m in call
        if m.role == "tool" and "[tool output folded" in m.content_text
    ]
    assert folded
    assert "excerpt: " in folded[0]
    assert len(folded[0]) < 300  # the 3000-char blob is gone
    # and the loop still completed
    assert events[-1].data["status"] == "completed"


@pytest.mark.asyncio
async def test_over_threshold_summarizes_middle_when_still_over() -> None:
    # Big *assistant* content (not foldable like tool results) forces the
    # summarize-middle path once folding alone can't get under threshold.
    big = "z" * 4_000
    provider = make_provider(
        [
            {"content": big, "tool_calls": [tc("emit")]},
            {"content": big, "tool_calls": [tc("emit")]},
            {"content": "final"},
        ]
    )
    router = ToolRouter()

    async def emit() -> str:
        return "ok"

    router.register(emit)
    # No summarizer configured → deterministic excerpt fallback.
    hooks = CompactionHooks(
        max_context_tokens=1_200,
        threshold_ratio=0.5,
        keep_last_messages=2,
        keep_last_tool_results=0,
    )
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    assert hooks.compactions >= 1
    summarized_seen = any(
        "[context compacted" in m.content_text
        for call in provider.calls[1:]
        for m in call
    )
    assert summarized_seen
    assert events[-1].data["status"] == "completed"


def test_pressure_blends_observed_usage_with_delta_estimate() -> None:
    hooks = CompactionHooks(max_context_tokens=60_000)
    transcript = [
        LLMMessage.text_of("user", "a" * 400),  # ~108 est
        LLMMessage.text_of("assistant", "b" * 40),
        LLMMessage.text_of("tool", "c" * 40, name="t", tool_call_id="c"),
    ]

    class _Ctx:
        last_prompt_tokens = None
        last_prompt_transcript_len = 0

    # No observation → full heuristic.
    assert hooks._pressure(transcript, _Ctx()) == hooks._estimate(transcript)
    # Observation covers the first message; only the appended two estimate.
    _Ctx.last_prompt_tokens = 5_000
    _Ctx.last_prompt_transcript_len = 1
    expected = 5_000 + hooks._estimate(transcript[1:])
    assert hooks._pressure(transcript, _Ctx()) == expected
    # Stale observation (transcript rewritten shorter) → full heuristic.
    _Ctx.last_prompt_transcript_len = 99
    assert hooks._pressure(transcript, _Ctx()) == hooks._estimate(transcript)


@pytest.mark.asyncio
async def test_observed_usage_overrides_heuristic_overestimate() -> None:
    # Transcript the heuristic scores OVER threshold (4k chars ≈ 1008 > 800),
    # but the provider measured the real prompt at 100 and nothing was
    # appended since — ground truth wins, no compaction. This is the
    # production 41%-overestimate class (dogfood: 22 compacts / 5 traces).
    hooks = CompactionHooks(max_context_tokens=1_000, threshold_ratio=0.8)
    transcript = [LLMMessage.text_of("user", "x" * 4_000)]

    class _Ctx:
        last_prompt_tokens = 100
        last_prompt_transcript_len = 1

    action = await hooks.pre_step(transcript, _Ctx())
    assert hooks.compactions == 0
    assert action.rewrite is None


@pytest.mark.asyncio
async def test_observed_usage_triggers_compaction_when_heuristic_calm() -> None:
    # Inverse: tiny messages (heuristic ≈ 60, calm) but the provider reports
    # the real prompt at 2000 against a 1600 threshold — compaction fires.
    from steerable_agent_runtime.llm import LLMUsage

    usage = LLMUsage(prompt_tokens=2_000, completion_tokens=5, total_tokens=2_005)
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("emit")], "usage": usage},
            {"content": "final", "usage": usage},
        ]
    )
    router = ToolRouter()

    async def emit() -> str:
        return "ok"

    router.register(emit)
    hooks = CompactionHooks(max_context_tokens=2_000, threshold_ratio=0.8)
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    assert hooks.compactions >= 1
    assert events[-1].data["status"] == "completed"


@pytest.mark.asyncio
async def test_hysteresis_blocks_recompaction_without_pressure_growth() -> None:
    # A huge assistant message lands in the protected tail (keep_last_messages
    # covers it), so no compaction can get pressure under threshold. Without
    # hysteresis pre_step re-compacts EVERY round (each rewrite destroys the
    # prompt-cache prefix); with it, compaction refires only after pressure
    # grows by the margin.
    provider = make_provider(
        [
            {"content": "z" * 8_000, "tool_calls": [tc("emit", {"n": 1})]},
            {"content": "s1", "tool_calls": [tc("emit", {"n": 2})]},
            {"content": "s2", "tool_calls": [tc("emit", {"n": 3})]},
            {"content": "final"},
        ]
    )
    router = ToolRouter()

    async def emit(n: int) -> str:
        return "ok"

    router.register(emit)
    hooks = CompactionHooks(
        max_context_tokens=1_000,
        threshold_ratio=0.5,
        keep_last_messages=6,
        keep_last_tool_results=0,
        recompact_margin_ratio=0.2,
    )
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    assert hooks.compactions == 1
    assert events[-1].data["status"] == "completed"


@pytest.mark.asyncio
async def test_compaction_resets_observed_state() -> None:
    # After a rewrite the observed indices are stale; the hook must clear them
    # so the next pressure check re-estimates (and the next request re-observes).
    hooks = CompactionHooks(
        max_context_tokens=1_000, threshold_ratio=0.5, keep_last_tool_results=0
    )
    transcript = [
        LLMMessage.text_of("user", "go"),
        LLMMessage.text_of("tool", "y" * 3_000, name="t", tool_call_id="c1"),
        LLMMessage.text_of("tool", "y" * 3_000, name="t", tool_call_id="c2"),
    ]

    class _Ctx:
        last_prompt_tokens = 900
        last_prompt_transcript_len = 3

    ctx = _Ctx()
    action = await hooks.pre_step(transcript, ctx)
    assert hooks.compactions == 1
    assert ctx.last_prompt_tokens is None
    assert ctx.last_prompt_transcript_len == 0
    assert action.rewrite is not None
    assert any("[tool output folded" in m.content_text for m in action.rewrite.messages)


@pytest.mark.asyncio
async def test_compaction_preserves_system_and_first_user_message() -> None:
    big = "w" * 4_000
    provider = make_provider(
        [
            {"content": big, "tool_calls": [tc("emit")]},
            {"content": big, "tool_calls": [tc("emit")]},
            {"content": "final"},
        ]
    )
    router = ToolRouter()

    async def emit() -> str:
        return "ok"

    router.register(emit)
    hooks = CompactionHooks(
        max_context_tokens=1_200,
        threshold_ratio=0.5,
        keep_last_messages=2,
        keep_last_tool_results=0,
    )
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    await collect(
        loop.run(
            [
                LLMMessage.text_of("system", "you are helpful"),
                LLMMessage.text_of("user", "the original goal"),
            ]
        )
    )

    # every call after compaction still starts with system + original goal
    for call in provider.calls[1:]:
        assert call[0].role == "system" and call[0].content_text == "you are helpful"
        assert call[1].role == "user" and call[1].content_text == "the original goal"
