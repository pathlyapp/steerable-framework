"""CompactionHooks: context-pressure-driven transcript rewriting (pre_step)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime import (
    ChainHooks,
    CompactionHooks,
    CoreLoop,
    LoopEvent,
    RouterToolExecutor,
    ToolRouter,
    estimate_tokens,
)
from steerable_agent_runtime.compaction import _fold_content
from steerable_agent_runtime.llm.errors import LLMError
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk


def test_fold_content_keeps_head_and_tail() -> None:
    text = "HEAD-START" + ("x" * 400) + "TAIL-METRIC-0.549"
    folded = _fold_content(text, excerpt_chars=80)
    assert "HEAD-START" in folded
    assert "TAIL-METRIC-0.549" in folded
    assert "truncated" in folded


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


async def test_over_threshold_uses_configured_summarizer() -> None:
    # With a summarizer wired (the sidecar default now that the framework owns
    # cross-turn compaction), the middle-summary content comes from the model,
    # not the deterministic excerpt fallback.
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

    class _Summarizer:
        name = "summarizer"
        model = "summarizer-model"

        def __init__(self) -> None:
            self.calls: list[list[LLMMessage]] = []

        async def complete(self, messages, *, cache_retention=None, **kw):
            self.calls.append(list(messages))
            return LLMMessage.text_of("assistant", "MODEL_SUMMARY"), None

    summarizer = _Summarizer()
    hooks = CompactionHooks(
        max_context_tokens=1_200,
        threshold_ratio=0.5,
        keep_last_messages=2,
        keep_last_tool_results=0,
        summarizer=summarizer,
    )
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    assert hooks.compactions >= 1
    # The summarizer was actually consulted for the discarded middle.
    assert summarizer.calls
    # The compacted transcript carries the model's summary, not the excerpt.
    summarized_seen = any(
        "MODEL_SUMMARY" in m.content_text
        for call in provider.calls[1:]
        for m in call
    )
    assert summarized_seen
    assert events[-1].data["status"] == "completed"


@pytest.mark.asyncio
async def test_summarizer_transport_error_falls_back_to_excerpt() -> None:
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

    class _Boom:
        name = "summarizer"
        model = "summarizer-model"

        async def complete(self, messages, *, cache_retention=None, **kw):
            raise ConnectionError("incomplete chunked read")

    hooks = CompactionHooks(
        max_context_tokens=1_200,
        threshold_ratio=0.5,
        keep_last_messages=2,
        keep_last_tool_results=0,
        summarizer=_Boom(),
    )
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))
    assert hooks.compactions >= 1
    assert events[-1].data["status"] == "completed"
    excerpt_seen = any(
        "[assistant]" in m.content_text or "[user]" in m.content_text
        for call in provider.calls[1:]
        for m in call
    )
    assert excerpt_seen


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


def tool_result(call_id: str) -> LLMMessage:
    return LLMMessage.text_of("tool", "ok", name="emit", tool_call_id=call_id)


@pytest.mark.asyncio
async def test_summarize_middle_widens_the_tail_off_an_orphan_tool_result() -> None:
    # A count-only tail cut can land inside a tool-call group: the issuing
    # assistant goes into the summarized middle while its results stay in the
    # tail, and the request opens on a `tool` message nothing answered.
    # DeepSeek rejects that with HTTP 400 invalid_request.
    calls = [
        ToolCall(id="call_a", name="emit", arguments={}),
        ToolCall(id="call_b", name="emit", arguments={}),
    ]
    transcript = [
        LLMMessage.text_of("system", "you are helpful"),
        LLMMessage.text_of("user", "make a deck"),
        LLMMessage.text_of("assistant", "reading the skill files"),
        LLMMessage.text_of("assistant", "", tool_calls=calls),
        tool_result("call_a"),
        # the read path interleaves image messages between tool results
        LLMMessage.text_of("user", "slide.png:"),
        tool_result("call_b"),
        LLMMessage.text_of("assistant", "wrapping up"),
    ]
    hooks = CompactionHooks(max_context_tokens=1_000, keep_last_messages=3)

    out = await hooks._summarize_middle(transcript)

    assert CompactionHooks._orphan_tool_ids(out) == set()
    assert any("[context compacted" in m.content_text for m in out)
    # the group survived whole, in order, with its interleaved image message
    assert [m.role for m in out[-5:]] == [
        "assistant",
        "tool",
        "user",
        "tool",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_summarize_middle_still_compacts_an_already_orphaned_transcript() -> None:
    # The issuer is missing from the input (a host resuming from a truncated
    # log), so widening could never repair the tail. Compaction proceeds
    # rather than widening to the head and going silent for the rest of the
    # run — the alternative to the provider 400 is a context overflow.
    transcript = [
        LLMMessage.text_of("user", "make a deck"),
        LLMMessage.text_of("assistant", "first"),
        tool_result("call_gone_a"),
        tool_result("call_gone_b"),
        LLMMessage.text_of("assistant", "wrapping up"),
    ]
    hooks = CompactionHooks(max_context_tokens=1_000, keep_last_messages=2)

    out = await hooks._summarize_middle(transcript)

    assert len(out) < len(transcript)
    assert any("[context compacted" in m.content_text for m in out)


@pytest.mark.asyncio
async def test_summarize_middle_keeps_an_aligned_tail_at_keep_last() -> None:
    # No tool-call group is cut, so the tail stays exactly keep_last long —
    # widening must not cost compaction on a clean boundary.
    transcript = [
        LLMMessage.text_of("system", "you are helpful"),
        LLMMessage.text_of("user", "make a deck"),
        LLMMessage.text_of("assistant", "first"),
        LLMMessage.text_of("assistant", "second"),
        LLMMessage.text_of("user", "keep going"),
        LLMMessage.text_of("assistant", "third"),
    ]
    hooks = CompactionHooks(max_context_tokens=1_000, keep_last_messages=3)

    out = await hooks._summarize_middle(transcript)

    assert [m.content_text for m in out[-3:]] == ["second", "keep going", "third"]
    assert len(out) == 6  # system + goal + summary + 3 kept


@pytest.mark.asyncio
async def test_micro_compact_trigger_schedule() -> None:
    """Periodic prune fires on interval multiples (never round 0), and an
    already-folded transcript is a no-op — folding is idempotent."""
    from types import SimpleNamespace

    hooks = CompactionHooks(
        max_context_tokens=1_000_000,  # pressure never fires
        micro_compact_interval_rounds=2,
    )
    transcript = [
        LLMMessage.text_of("user", "goal"),
        *[
            LLMMessage.text_of("tool", "x" * 500, name="emit", tool_call_id=f"c{i}")
            for i in range(4)
        ],
        LLMMessage.text_of("assistant", "working"),
    ]
    assert (
        await hooks.pre_step(transcript, SimpleNamespace(round_index=0))
    ).rewrite is None
    assert (
        await hooks.pre_step(transcript, SimpleNamespace(round_index=1))
    ).rewrite is None
    action = await hooks.pre_step(transcript, SimpleNamespace(round_index=2))
    assert action.rewrite is not None
    assert action.rewrite.action == "micro_compact"
    assert hooks.micro_compactions == 1
    assert hooks.compactions == 1
    # Second fire on the pruned transcript: nothing left to fold → no rewrite
    # (and no pointless prompt-cache invalidation).
    again = await hooks.pre_step(
        action.rewrite.messages, SimpleNamespace(round_index=4)
    )
    assert again.rewrite is None
    assert hooks.micro_compactions == 1


@pytest.mark.asyncio
async def test_micro_compact_default_off() -> None:
    from types import SimpleNamespace

    hooks = CompactionHooks(max_context_tokens=1_000_000)
    transcript = [
        LLMMessage.text_of("user", "goal"),
        *[
            LLMMessage.text_of("tool", "x" * 500, name="emit", tool_call_id=f"c{i}")
            for i in range(4)
        ],
    ]
    action = await hooks.pre_step(transcript, SimpleNamespace(round_index=4))
    assert action.rewrite is None
    assert hooks.micro_compactions == 0


@pytest.mark.asyncio
async def test_micro_compact_folds_periodically_under_no_pressure() -> None:
    """Loop level: with the interval set, old tool results get folded on
    schedule even though pressure never crosses the threshold."""
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
        max_context_tokens=1_000_000,  # pressure never fires
        keep_last_tool_results=1,
        micro_compact_interval_rounds=2,
    )
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    await collect(loop.run([LLMMessage.text_of("user", "go")]))

    assert hooks.micro_compactions >= 1
    # Every compaction here was the periodic one — pressure stayed under.
    assert hooks.compactions == hooks.micro_compactions
    seen_folded = any(
        "[tool output folded" in m.content_text
        for call in provider.calls[1:]
        for m in call
        if m.role == "tool"
    )
    assert seen_folded


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_three_ineffective_compactions() -> None:
    # A kept-last tool result bigger than the threshold makes every pressure
    # compaction ineffective (fold+summarize still lands over). Three in a
    # row open the circuit; the pressure path then stops firing.
    hooks = CompactionHooks(
        max_context_tokens=200,
        threshold_ratio=0.8,
        keep_last_messages=2,
        keep_last_tool_results=1,
        recompact_margin_ratio=0.0,  # fire every round while over
    )

    class _Ctx:
        round_index = 0

    big_tool = LLMMessage.text_of("tool", "y" * 3_000, name="emit", tool_call_id="t1")
    transcript = [LLMMessage.text_of("user", "go"), big_tool]

    for round_index in range(1, 5):
        _Ctx.round_index = round_index
        action = await hooks.pre_step(list(transcript), _Ctx())
        if round_index <= 3:
            # Each pass rewrites (best effort) but stays over threshold.
            assert action.rewrite is not None
            assert action.rewrite.pre_tokens is not None
            assert action.rewrite.post_tokens is not None
            assert action.rewrite.post_tokens >= 0.8 * 200
        else:
            # Breaker open: no more pointless rewrites.
            assert action.rewrite is None
    assert hooks.circuit_open is True
    assert hooks.compactions == 3


def _refill_transcript(base: list[LLMMessage], round_index: int, n_new: int = 2) -> list[LLMMessage]:
    """Append fresh tool results so the transcript climbs back over
    threshold — the rapid-refill pattern: each compaction's freed space is
    eaten by new verbose output within a round."""
    msgs = list(base)
    for i in range(n_new):
        msgs.append(
            LLMMessage.text_of(
                "tool", "x" * 240, name="emit", tool_call_id=f"r{round_index}n{i}"
            )
        )
    return msgs


@pytest.mark.asyncio
async def test_rapid_refill_breaker_opens_and_warns() -> None:
    # CC parity: three compactions in a row whose freed space refills within
    # 3 rounds open the circuit; the tripping round carries the thrashing
    # reminder, and the pressure path then stops firing.
    hooks = CompactionHooks(
        max_context_tokens=200,
        threshold_ratio=0.8,
        keep_last_messages=6,
        keep_last_tool_results=1,
        # Small excerpts so a fold actually lands under the tiny test
        # threshold (the default 160-char excerpt is ~60 tokens each).
        fold_excerpt_chars=40,
        recompact_margin_ratio=0.0,  # isolate the refill breaker from hysteresis
    )

    class _Ctx:
        round_index = 0

    saw_reminder = False
    for round_index in range(1, 6):
        _Ctx.round_index = round_index
        # A fresh over-threshold transcript each round: the last compaction's
        # freed space was eaten by new verbose output (the refill pattern).
        transcript = _refill_transcript([LLMMessage.text_of("user", "go")], round_index, n_new=3)
        action = await hooks.pre_step(transcript, _Ctx())
        if round_index <= 4:
            # Every pass compacts successfully (fold lands under threshold)…
            assert action.rewrite is not None
            assert action.rewrite.post_tokens is not None
            assert action.rewrite.post_tokens < 0.8 * 200
            if round_index == 4:
                # …and the third consecutive refill trips the breaker with
                # the actionable reminder appended.
                assert action.appends is not None
                fragment = action.appends[0].fragment
                assert fragment is not None
                assert fragment.content_kind == "reminder.compaction_thrashing"
                assert "thrashing" in action.appends[0].message.content_text
                saw_reminder = True
        else:
            # Breaker open: pressure path stops firing despite the refill.
            assert action.rewrite is None
    assert saw_reminder
    assert hooks.circuit_open is True
    assert hooks.circuit_reason == "rapid_refill"
    assert hooks.compactions == 4


@pytest.mark.asyncio
async def test_rapid_refill_count_resets_on_a_healthy_round() -> None:
    hooks = CompactionHooks(
        max_context_tokens=200,
        threshold_ratio=0.8,
        keep_last_messages=6,
        keep_last_tool_results=1,
        fold_excerpt_chars=40,
        recompact_margin_ratio=0.0,
    )

    class _Ctx:
        round_index = 0

    # Two consecutive refills…
    for round_index in (1, 2):
        _Ctx.round_index = round_index
        transcript = _refill_transcript([LLMMessage.text_of("user", "go")], round_index, n_new=3)
        action = await hooks.pre_step(transcript, _Ctx())
        assert action.rewrite is not None
    assert hooks._rapid_refills == 1  # first compaction is the baseline, not a refill

    # …then a healthy round breaks the streak…
    _Ctx.round_index = 3
    small = [LLMMessage.text_of("user", "go")]
    action = await hooks.pre_step(small, _Ctx())
    assert action.rewrite is None
    assert hooks._rapid_refills == 0

    # …so two further refills do not trip the breaker.
    for round_index in (4, 5):
        _Ctx.round_index = round_index
        transcript = _refill_transcript([LLMMessage.text_of("user", "go")], round_index, n_new=3)
        action = await hooks.pre_step(transcript, _Ctx())
        assert action.rewrite is not None
    assert hooks.circuit_open is False


@pytest.mark.asyncio
async def test_compact_now_resets_the_refill_streak() -> None:
    hooks = CompactionHooks(
        max_context_tokens=200,
        threshold_ratio=0.8,
        keep_last_messages=6,
        keep_last_tool_results=1,
        fold_excerpt_chars=40,
        recompact_margin_ratio=0.0,
    )

    class _Ctx:
        round_index = 0

    transcript: list[LLMMessage] = []
    for round_index in (1, 2, 3):
        _Ctx.round_index = round_index
        transcript = _refill_transcript([LLMMessage.text_of("user", "go")], round_index, n_new=3)
        action = await hooks.pre_step(transcript, _Ctx())
        assert action.rewrite is not None
    assert hooks._rapid_refills == 2

    # The user takes over: manual compact clears the streak and the history.
    await hooks.compact_now(list(transcript), _Ctx())
    assert hooks._rapid_refills == 0
    assert hooks._last_compact_round is None


@pytest.mark.asyncio
async def test_rapid_refill_window_boundary() -> None:
    # A compaction exactly `window` rounds after the previous one counts as a
    # refill; one round later does not — the streak restarts instead.
    hooks = CompactionHooks(
        max_context_tokens=200,
        threshold_ratio=0.8,
        keep_last_messages=6,
        keep_last_tool_results=1,
        fold_excerpt_chars=40,
        recompact_margin_ratio=0.0,
    )

    class _Ctx:
        round_index = 0

    async def _compact_at(round_index: int) -> None:
        _Ctx.round_index = round_index
        transcript = _refill_transcript([LLMMessage.text_of("user", "go")], round_index, n_new=3)
        action = await hooks.pre_step(transcript, _Ctx())
        assert action.rewrite is not None

    await _compact_at(1)
    assert hooks._rapid_refills == 0  # baseline
    await _compact_at(4)  # exactly 3 rounds later → inside the window
    assert hooks._rapid_refills == 1
    await _compact_at(8)  # 4 rounds later → outside the window, streak restarts
    assert hooks._rapid_refills == 0
    await _compact_at(11)  # 3 rounds later → inside again
    assert hooks._rapid_refills == 1
    assert hooks.circuit_open is False


@pytest.mark.asyncio
async def test_ineffective_compaction_is_not_a_refill_baseline() -> None:
    # A compaction that stays over threshold belongs to the failure breaker;
    # it must not move the rapid-refill baseline, or a later successful
    # compaction would be miscounted as a refill of a compaction that never
    # freed anything.
    hooks = CompactionHooks(
        max_context_tokens=200,
        threshold_ratio=0.8,
        keep_last_messages=2,
        keep_last_tool_results=1,
        recompact_margin_ratio=0.0,
    )

    class _Ctx:
        round_index = 0

    # Round 1: an ineffective compaction (kept-last tool result alone is
    # bigger than the threshold).
    _Ctx.round_index = 1
    big = [
        LLMMessage.text_of("user", "go"),
        LLMMessage.text_of("tool", "y" * 3_000, name="e", tool_call_id="t"),
    ]
    action = await hooks.pre_step(list(big), _Ctx())
    assert action.rewrite is not None
    assert hooks._consecutive_failures == 1
    assert hooks._last_compact_round is None  # failure is not a refill baseline

    # Round 2: a successful compaction right after — still the baseline,
    # not a refill of the failed one.
    _Ctx.round_index = 2
    hooks_small = CompactionHooks(
        max_context_tokens=200,
        threshold_ratio=0.8,
        keep_last_messages=6,
        keep_last_tool_results=1,
        fold_excerpt_chars=40,
        recompact_margin_ratio=0.0,
    )
    hooks_small._consecutive_failures = hooks._consecutive_failures
    transcript = _refill_transcript([LLMMessage.text_of("user", "go")], 2, n_new=3)
    action = await hooks_small.pre_step(transcript, _Ctx())
    assert action.rewrite is not None
    assert hooks_small._rapid_refills == 0


@pytest.mark.asyncio
async def test_micro_compaction_does_not_count_as_refill() -> None:
    # Micro-compaction is a scheduled prune, not a pressure response; it must
    # not feed the refill streak (its cadence would trip the breaker on
    # healthy transcripts).
    hooks = CompactionHooks(
        max_context_tokens=10_000_000,  # pressure never fires
        micro_compact_interval_rounds=1,
        keep_last_tool_results=1,
    )

    class _Ctx:
        round_index = 0

    transcript = _refill_transcript([LLMMessage.text_of("user", "go")], 0, n_new=3)
    for round_index in range(1, 5):
        _Ctx.round_index = round_index
        action = await hooks.pre_step(list(transcript), _Ctx())
        assert action.rewrite is not None  # micro fold fires every round
    assert hooks.micro_compactions == 4
    assert hooks._rapid_refills == 0
    assert hooks._last_compact_round is None
    assert hooks.circuit_open is False


@pytest.mark.asyncio
async def test_failure_breaker_records_its_reason() -> None:
    hooks = CompactionHooks(
        max_context_tokens=200,
        threshold_ratio=0.8,
        keep_last_messages=2,
        keep_last_tool_results=1,
        recompact_margin_ratio=0.0,
    )

    class _Ctx:
        round_index = 0

    big = [
        LLMMessage.text_of("user", "go"),
        LLMMessage.text_of("tool", "y" * 3_000, name="e", tool_call_id="t"),
    ]
    for round_index in range(1, 4):
        _Ctx.round_index = round_index
        await hooks.pre_step(list(big), _Ctx())
    assert hooks.circuit_open is True
    assert hooks.circuit_reason == "consecutive_failures"


@pytest.mark.asyncio
async def test_overflow_path_stays_live_with_the_circuit_open() -> None:
    # Breaker open only silences the pressure path; a real context-overflow
    # error must still force a compaction and retry (bounded), so the turn
    # fails loud instead of spinning.
    hooks = CompactionHooks(
        max_context_tokens=200,
        threshold_ratio=0.8,
        keep_last_messages=6,
        keep_last_tool_results=1,
        fold_excerpt_chars=40,
        recompact_margin_ratio=0.0,
    )
    hooks.circuit_open = True
    hooks.circuit_reason = "rapid_refill"

    class _Ctx:
        round_index = 7

    transcript = _refill_transcript([LLMMessage.text_of("user", "go")], 7, n_new=3)
    action = await hooks.on_request_error(
        LLMError("prompt too long", kind="context_overflow", status_code=400),
        transcript,
        _Ctx(),
    )
    assert action.kind == "retry"
    assert action.rewrite is not None
    assert hooks.overflow_recoveries == 1


@pytest.mark.asyncio
async def test_circuit_breaker_resets_on_a_healthy_round() -> None:
    hooks = CompactionHooks(
        max_context_tokens=200,
        threshold_ratio=0.8,
        keep_last_messages=2,
        keep_last_tool_results=1,
        recompact_margin_ratio=0.0,
    )

    class _Ctx:
        round_index = 0

    big = [LLMMessage.text_of("user", "go"), LLMMessage.text_of("tool", "y" * 3_000, name="e", tool_call_id="t")]
    small = [LLMMessage.text_of("user", "go"), LLMMessage.text_of("tool", "ok", name="e", tool_call_id="t")]

    await hooks.pre_step(list(big), _Ctx())
    await hooks.pre_step(list(big), _Ctx())
    assert hooks._consecutive_failures == 2
    await hooks.pre_step(list(small), _Ctx())  # under threshold → reset
    assert hooks._consecutive_failures == 0
    assert hooks.circuit_open is False


@pytest.mark.asyncio
async def test_compact_now_manual_bypasses_threshold_and_breaker() -> None:
    hooks = CompactionHooks(
        max_context_tokens=1_000_000,  # pressure never fires on its own
        keep_last_messages=2,
        keep_last_tool_results=1,
    )
    hooks.circuit_open = True  # manual compact ignores the breaker

    class _Ctx:
        round_index = 0
        last_prompt_tokens = None
        last_prompt_transcript_len = 0

    transcript = [LLMMessage.text_of("user", "go")]
    for i in range(4):
        transcript.append(
            LLMMessage.text_of("tool", f"result-{i} " + "x" * 500, name="emit", tool_call_id=f"t{i}")
        )
    action = await hooks.compact_now(list(transcript), _Ctx())
    assert action.rewrite is not None
    assert action.rewrite.action == "compact"
    assert action.rewrite.reason == "manual compact"
    assert action.rewrite.pre_tokens is not None
    assert action.rewrite.post_tokens is not None
    assert action.rewrite.post_tokens < action.rewrite.pre_tokens
    # Fold ran (old results carry the fold marker inside the summary span or
    # in the tail) and the middle was summarized away: 5 messages shrink to
    # head + summary + tail.
    assert len(action.rewrite.messages) < len(transcript)
    assert any(
        m.role == "user" and m.content_text.startswith("[context compacted")
        for m in action.rewrite.messages
    )
    assert hooks.compactions == 1


@pytest.mark.asyncio
async def test_compact_now_on_a_minimal_transcript_is_a_noop() -> None:
    hooks = CompactionHooks(max_context_tokens=1_000_000)

    class _Ctx:
        round_index = 0
        last_prompt_tokens = None
        last_prompt_transcript_len = 0

    transcript = [LLMMessage.text_of("user", "go")]
    action = await hooks.compact_now(list(transcript), _Ctx())
    assert action.rewrite is None
    assert hooks.compactions == 0


@pytest.mark.asyncio
async def test_loop_request_compact_rewrites_transcript_mid_run() -> None:
    """CoreLoop.request_compact() (host /compact parity) is consumed at the
    next pre_step boundary: the manual pass folds + summarizes regardless of
    pressure and surfaces as a hook_action event with action="compact"."""
    tool_started = asyncio.Event()
    proceed = asyncio.Event()
    big = "y" * 3_000
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("emit", {"n": 1})]},
            {"content": "", "tool_calls": [tc("emit", {"n": 2})]},
            {"content": "final"},
        ]
    )
    router = ToolRouter()

    async def emit(n: int) -> str:
        if n == 2:
            tool_started.set()
            await proceed.wait()  # hold the turn open so the compact lands mid-run
        return big

    router.register(emit)
    compaction = CompactionHooks(
        max_context_tokens=1_000_000,  # pressure never fires on its own
        keep_last_messages=2,
        keep_last_tool_results=1,
    )
    loop = CoreLoop(
        provider, RouterToolExecutor(router), hooks=ChainHooks(compaction)
    )

    task = asyncio.create_task(collect(loop.run([LLMMessage.text_of("user", "go")])))
    await asyncio.wait_for(tool_started.wait(), timeout=2)
    loop.request_compact()
    proceed.set()
    events = await asyncio.wait_for(task, timeout=2)

    # Exactly one compaction happened, and it was the manual one.
    assert compaction.compactions == 1
    compact_events = [
        e
        for e in events
        if e.kind == "hook_action" and e.data.get("action") == "compact"
    ]
    assert len(compact_events) == 1
    assert compact_events[0].data["reason"] == "manual compact"
    # The final model call saw the compacted transcript (fold + summary).
    assert any(
        "[context compacted" in m.content_text
        or "[tool output folded" in m.content_text
        for m in provider.calls[-1]
    )
    assert events[-1].data["status"] == "completed"


@pytest.mark.asyncio
async def test_loop_request_compact_without_compaction_hook_is_noop() -> None:
    # Default NoopHooks has no compact_now: the request is consumed at the
    # pre_step boundary and quietly does nothing.
    provider = make_provider([{"content": "answer"}])
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()))
    loop.request_compact()
    events = await collect(loop.run([LLMMessage.text_of("user", "hi")]))
    assert events[-1].data["status"] == "completed"
    assert provider.calls[0][0].content_text == "hi"
    assert not [e for e in events if e.kind == "hook_action"]


@pytest.mark.asyncio
async def test_hook_action_event_carries_boundary_token_counts() -> None:
    # The loop threads the rewriter's estimates onto the hook_action event
    # (and the recorded boundary) — CC compact_boundary pre/post parity.
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
        max_context_tokens=2_000,
        threshold_ratio=0.8,
        keep_last_messages=4,
        keep_last_tool_results=1,
    )
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    compactions = [
        e for e in events
        if e.kind == "hook_action" and e.data.get("action") == "compact"
    ]
    assert compactions
    for event in compactions:
        assert isinstance(event.data["pre_tokens"], int)
        assert isinstance(event.data["post_tokens"], int)
        assert event.data["post_tokens"] < event.data["pre_tokens"]
