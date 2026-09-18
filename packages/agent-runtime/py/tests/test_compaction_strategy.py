"""Compaction strategy standard (L1 mechanism tests).

The comparison-doc compaction axis rates maturity by verifiable mechanism,
not feature claims. This file pins the four mechanism properties that
separate a strong strategy from a naive one:

1. Warm-prefix replay — the summarizer request is the conversation's own
   head + the shadowed span verbatim + the instruction as the final user
   message, i.e. a byte-prefix of the last conversation request (dsh
   compaction-basic parity). Only the instruction and the summary output
   miss the provider prompt cache.
2. No silent truncation — every byte of a shadowed message reaches the
   summarizer (the old ``[:2000]`` flattening discarded exactly the content
   being summarized).
3. Raw-span replay — the summarizer sees the span as the provider last saw
   it; the fold that precedes summarization rewrites only the post-compaction
   projection, never the replay (folding would break byte-identity with the
   cached prefix at the first folded message).
4. Prune-before-summarize — when folding alone relieves the pressure, the
   summarizer is never called (the LLM call is the expensive stage).

Breaker / hysteresis / micro-compaction / overflow machinery is covered in
test_compaction.py; this file only covers the summarization contract.
"""

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
)
from steerable_agent_runtime.compaction import _SUMMARY_INSTRUCTION
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


class _Summarizer:
    """Records every request and answers with a fixed summary."""

    name = "summarizer"
    model = "summarizer-model"

    def __init__(self, reply: str = "MODEL_SUMMARY") -> None:
        self.calls: list[list[LLMMessage]] = []
        self.kwargs: list[dict[str, Any]] = []
        self._reply = reply

    async def complete(self, messages, *, cache_retention=None, **kw):
        self.calls.append(list(messages))
        self.kwargs.append({"cache_retention": cache_retention, **kw})
        return LLMMessage.text_of("assistant", self._reply), None


def tc(name: str, args: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(id=f"call_{name}", name=name, arguments=args or {})


async def collect(loop_run: AsyncIterator[LoopEvent]) -> list[LoopEvent]:
    return [e async for e in loop_run]


def _big_assistant_script(big: str) -> list[dict[str, Any]]:
    """Two rounds of un-foldable big assistant content → summarize path."""
    return [
        {"content": big, "tool_calls": [tc("emit", {"n": 1})]},
        {"content": big, "tool_calls": [tc("emit", {"n": 2})]},
        {"content": "final"},
    ]


@pytest.mark.asyncio
async def test_summarizer_request_replays_conversation_prefix() -> None:
    big = "z" * 4_000
    provider = make_provider(_big_assistant_script(big))
    router = ToolRouter()

    async def emit(n: int) -> str:
        return "ok"

    router.register(emit)
    summarizer = _Summarizer()
    hooks = CompactionHooks(
        max_context_tokens=1_200,
        threshold_ratio=0.5,
        keep_last_messages=2,
        keep_last_tool_results=0,
        summarizer=summarizer,
    )
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    system = LLMMessage.text_of("system", "You are a careful coding agent.")
    goal = LLMMessage.text_of("user", "修复登录页的白屏")
    events = await collect(loop.run([system, goal]))

    assert events[-1].data["status"] == "completed"
    assert summarizer.calls, "summarizer was never consulted"
    request = summarizer.calls[0]

    # 1. The request opens with the conversation's own system message and
    #    first user message (the goal) — no foreign "Summarize this…" system.
    assert request[0].role == "system"
    assert request[0].content_text == "You are a careful coding agent."
    assert request[1].role == "user"
    assert request[1].content_text == "修复登录页的白屏"

    # 2. The shadowed span is replayed verbatim: real roles, full content,
    #    in conversation order — not a "[role] excerpt" flattening.
    replayed = request[2:-1]
    assert any(m.role == "assistant" and m.content_text == big for m in replayed)
    assert any(m.role == "tool" for m in replayed)
    assert not any(m.content_text.startswith("[assistant]") for m in replayed)

    # 3. The instruction is the final user message; nothing follows it.
    assert request[-1].role == "user"
    assert request[-1].content_text == _SUMMARY_INSTRUCTION


@pytest.mark.asyncio
async def test_summarizer_receives_long_messages_verbatim() -> None:
    # A 3000-char middle message must reach the summarizer intact — the old
    # ``[:2000]`` truncation silently discarded a third of it.
    big = "重要上下文-" + ("数" * 3_000) + "-关键结论"
    provider = make_provider(_big_assistant_script(big))
    router = ToolRouter()

    async def emit(n: int) -> str:
        return "ok"

    router.register(emit)
    summarizer = _Summarizer()
    hooks = CompactionHooks(
        max_context_tokens=1_200,
        threshold_ratio=0.5,
        keep_last_messages=2,
        keep_last_tool_results=0,
        summarizer=summarizer,
    )
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    await collect(loop.run([LLMMessage.text_of("user", "go")]))

    assert summarizer.calls
    replayed = summarizer.calls[0][1:-1]  # no system head in this transcript
    assert any(m.content_text == big for m in replayed), (
        "middle message was truncated before summarization"
    )


@pytest.mark.asyncio
async def test_summarizer_replays_raw_span_despite_preceding_fold() -> None:
    # Single-pass fold+summarize: the fold stage runs first and rewrites the
    # projection, but the summarizer must still replay the RAW span — that is
    # the byte-prefix the provider cached on the previous request. Observed
    # usage drives the pressure so round 1 stays calm and round 2 crosses the
    # threshold with the transcript still unfolded.
    from steerable_agent_runtime.llm import LLMUsage

    big_assistant = "a" * 20_000  # un-foldable, keeps post-fold estimate over
    blob = "y" * 3_000
    provider = make_provider(
        [
            {
                "content": big_assistant,
                "tool_calls": [tc("emit", {"n": 1})],
                "usage": LLMUsage(prompt_tokens=1_000, completion_tokens=5, total_tokens=1_005),
            },
            {
                "content": big_assistant,
                "tool_calls": [tc("emit", {"n": 2})],
                "usage": LLMUsage(prompt_tokens=9_000, completion_tokens=5, total_tokens=9_005),
            },
            {"content": "final"},
        ]
    )
    router = ToolRouter()

    async def emit(n: int) -> str:
        return blob

    router.register(emit)
    summarizer = _Summarizer()
    hooks = CompactionHooks(
        max_context_tokens=10_000,
        threshold_ratio=0.8,
        keep_last_messages=2,
        keep_last_tool_results=0,
        summarizer=summarizer,
    )
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    await collect(loop.run([LLMMessage.text_of("user", "go")]))

    assert summarizer.calls
    replay_texts = [m.content_text for m in summarizer.calls[0]]
    # Tool results arrive in the router's JSON envelope, so the blob is
    # contained in the replayed message rather than equal to it; the point
    # is that the FULL raw span is replayed, not the ~200-char fold marker.
    assert any(blob in t for t in replay_texts), (
        "summarizer saw the folded marker, not the raw span"
    )
    assert not any(t.startswith("[tool output folded") for t in replay_texts)

    # The rewritten projection carries the model summary for the shadowed
    # span, and the kept tail stays RAW — folding never reaches into it.
    final_request = provider.calls[-1]
    assert any("MODEL_SUMMARY" in m.content_text for m in final_request)
    assert any(blob in m.content_text for m in final_request)
    assert not any(
        m.content_text.startswith("[tool output folded") for m in final_request
    )


@pytest.mark.asyncio
async def test_fold_alone_relieving_pressure_skips_summarizer() -> None:
    # Prune-before-summarize: folding old tool results gets the estimate back
    # under threshold, so the LLM stage must not fire. The window must sit
    # above the kept floor (tail + kept middle results stay raw), so five
    # rounds accumulate before the threshold trips and one fold relieves.
    big = "y" * 3_000
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("emit", {"n": i})]}
            for i in range(1, 6)
        ]
        + [{"content": "final"}]
    )
    router = ToolRouter()

    async def emit(n: int) -> str:
        return big

    router.register(emit)
    summarizer = _Summarizer()
    hooks = CompactionHooks(
        max_context_tokens=4_000,
        threshold_ratio=0.8,
        keep_last_messages=2,
        keep_last_tool_results=1,
        summarizer=summarizer,
    )
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    assert hooks.compactions >= 1
    assert summarizer.calls == [], "folding relieved the pressure; summarizer must stay idle"
    assert events[-1].data["status"] == "completed"


@pytest.mark.asyncio
async def test_summarizer_replay_keeps_cache_retention_none() -> None:
    # The replayed prefix is already warm from the conversation's own
    # requests; retention=none only skips writing the discard-bound
    # instruction suffix as a new cache entry. Pin the kwarg so a future
    # change to "default" is a deliberate decision, not drift.
    big = "z" * 4_000
    provider = make_provider(_big_assistant_script(big))
    router = ToolRouter()

    async def emit(n: int) -> str:
        return "ok"

    router.register(emit)
    summarizer = _Summarizer()
    hooks = CompactionHooks(
        max_context_tokens=1_200,
        threshold_ratio=0.5,
        keep_last_messages=2,
        keep_last_tool_results=0,
        summarizer=summarizer,
    )
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    await collect(loop.run([LLMMessage.text_of("user", "go")]))

    assert summarizer.calls
    assert summarizer.kwargs[0]["cache_retention"] == "none"


# ---------------------------------------------------------------------------
# Region transaction (P1): the durable record carries the full bracket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_region_transaction_records_the_full_bracket() -> None:
    """A summarizing compaction lands start → summary → boundary in the
    durable record, sharing one compaction_id, with the span indices naming
    the shadowed middle of the pre-rewrite projection."""
    from steerable_agent_runtime.history import (
        CompactionBoundary,
        CompactionStart,
        CompactionSummary,
    )

    big = "w" * 4_000
    provider = make_provider(_big_assistant_script(big))
    router = ToolRouter()

    async def emit(n: int) -> str:
        return "ok"

    router.register(emit)
    hooks = CompactionHooks(
        max_context_tokens=1_200,
        threshold_ratio=0.5,
        keep_last_messages=2,
        keep_last_tool_results=0,
        summarizer=_Summarizer(),
    )
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    await collect(loop.run([LLMMessage.text_of("user", "go")]))

    record = loop.history.record
    starts = [e for e in record if isinstance(e, CompactionStart)]
    summaries = [e for e in record if isinstance(e, CompactionSummary)]
    boundaries = [e for e in record if isinstance(e, CompactionBoundary)]
    assert len(starts) >= 1 and len(summaries) == len(starts)

    for start in starts:
        cid = start.compaction_id
        # Ordered triplet: start < summary < boundary, all sharing the id.
        summary = next(s for s in summaries if s.compaction_id == cid)
        boundary = next(b for b in boundaries if b.compaction_id == cid)
        assert start.seq < summary.seq < boundary.seq
        # The span names a real middle of the pre-rewrite projection.
        assert start.span_start_index is not None and start.span_start_index >= 1
        assert start.span_end_index is not None
        assert start.span_end_index > start.span_start_index
        # The paid summary on the record is what the projection carries.
        assert summary.summary_text == "MODEL_SUMMARY"
        assert any(
            summary.summary_text in m.content_text
            for m in loop.history.projection
        )


@pytest.mark.asyncio
async def test_bracket_entries_are_projection_inert() -> None:
    """Start/summary entries are audit metadata: the model-visible
    projection never surfaces them."""
    from steerable_agent_runtime.history import CompactionStart, CompactionSummary

    big = "w" * 4_000
    provider = make_provider(_big_assistant_script(big))
    router = ToolRouter()

    async def emit(n: int) -> str:
        return "ok"

    router.register(emit)
    hooks = CompactionHooks(
        max_context_tokens=1_200,
        threshold_ratio=0.5,
        keep_last_messages=2,
        keep_last_tool_results=0,
        summarizer=_Summarizer(),
    )
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    await collect(loop.run([LLMMessage.text_of("user", "go")]))

    record = loop.history.record
    assert any(isinstance(e, CompactionStart) for e in record)
    assert any(isinstance(e, CompactionSummary) for e in record)
    # Every request the provider saw is bracket-free: projection only.
    for call in provider.calls:
        for m in call:
            assert not m.content_text.startswith("compaction.start")
            assert "compaction_id" not in m.content_text


@pytest.mark.asyncio
async def test_fold_only_compaction_records_a_bare_boundary() -> None:
    """No summarizer ran → no bracket: the boundary alone marks the
    rewrite, exactly like pre-P1 records."""
    from steerable_agent_runtime.history import (
        CompactionBoundary,
        CompactionStart,
        CompactionSummary,
    )

    big = "y" * 3_000
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("emit", {"n": i})]}
            for i in range(1, 6)
        ]
        + [{"content": "final"}]
    )
    router = ToolRouter()

    async def emit(n: int) -> str:
        return big

    router.register(emit)
    hooks = CompactionHooks(
        max_context_tokens=4_000,
        threshold_ratio=0.8,
        keep_last_messages=2,
        keep_last_tool_results=1,
        summarizer=None,
    )
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    await collect(loop.run([LLMMessage.text_of("user", "go")]))

    record = loop.history.record
    assert hooks.compactions >= 1
    assert not any(isinstance(e, CompactionStart) for e in record)
    assert not any(isinstance(e, CompactionSummary) for e in record)
    boundaries = [e for e in record if isinstance(e, CompactionBoundary)]
    assert boundaries and all(b.compaction_id is None for b in boundaries)


# ---------------------------------------------------------------------------
# Image offload (P2): old images shed bytes, the record keeps the originals
# ---------------------------------------------------------------------------


def _image_message(source: str, *, role: str = "user") -> LLMMessage:
    from steerable_agent_runtime.llm import ImagePart

    return LLMMessage(
        role=role,
        content=[ImagePart(source=source, is_url=False, media_type="image/png")],
    )


@pytest.mark.asyncio
async def test_old_images_offload_to_text_pointers_inside_the_horizon() -> None:
    """Pressure pass: images in the shadowed middle become text pointers;
    the newest image inside the horizon and the whole tail stay raw."""
    from steerable_agent_runtime.llm import ImagePart

    # Window above the post-offload floor (one kept image ≈ 1024 tokens) so
    # the prune stage alone relieves and the pointers persist in the
    # projection — a summarize pass would shadow them under the summary.
    hooks = CompactionHooks(
        max_context_tokens=2_000,
        threshold_ratio=0.8,
        keep_last_messages=2,
        keep_last_tool_results=0,
        keep_last_images=1,
        summarizer=_Summarizer(),
    )
    transcript = [
        LLMMessage.text_of("user", "look at these"),
        _image_message("img-1"),
        _image_message("img-2"),
        _image_message("img-3"),
        LLMMessage.text_of("assistant", "thinking"),
        LLMMessage.text_of("assistant", "wrapping up"),
    ]

    class _Ctx:
        round_index = 0
        last_prompt_tokens = None
        last_prompt_transcript_len = 0

    action = await hooks.pre_step(list(transcript), _Ctx())
    assert action.rewrite is not None
    out = action.rewrite.messages
    # img-1 and img-2 offloaded (oldest inside the horizon), img-3 kept raw
    # (newest image-bearer), the 2-message tail untouched.
    pointers = [
        m for m in out
        if any("[image offloaded to save context]" in getattr(p, "text", "")
               for p in m.content)
    ]
    assert len(pointers) == 2
    raw_images = [
        m for m in out
        if any(isinstance(p, ImagePart) for p in m.content)
    ]
    assert len(raw_images) == 1  # img-3 survives inside the horizon


@pytest.mark.asyncio
async def test_offloaded_image_originals_stay_in_the_record() -> None:
    """The projection sheds image bytes; the durable record keeps the
    original items (append-only) so nothing is actually lost."""
    from steerable_agent_runtime.history import HistoryItem
    from steerable_agent_runtime.llm import ImagePart

    big = "w" * 4_000
    provider = make_provider(_big_assistant_script(big))
    router = ToolRouter()

    async def emit(n: int) -> str:
        return "ok"

    router.register(emit)
    hooks = CompactionHooks(
        max_context_tokens=1_200,
        threshold_ratio=0.5,
        keep_last_messages=2,
        keep_last_tool_results=0,
        keep_last_images=0,
        summarizer=_Summarizer(),
    )
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    await collect(
        loop.run([LLMMessage.text_of("user", "go"), _image_message("img-0")])
    )

    # The record still holds the original image item…
    originals = [
        e
        for e in loop.history.record
        if isinstance(e, HistoryItem)
        and any(isinstance(p, ImagePart) for p in e.message.content)
    ]
    assert originals, "the original image must stay in the durable record"
    # …and the summarizer never saw image bytes — it got the text pointer.
    for call in hooks._summarizer.calls:  # type: ignore[union-attr]
        for m in call:
            assert not any(isinstance(p, ImagePart) for p in m.content)


@pytest.mark.asyncio
async def test_image_offload_disabled_keeps_everything_raw() -> None:
    from steerable_agent_runtime.llm import ImagePart

    hooks = CompactionHooks(
        max_context_tokens=200,
        threshold_ratio=0.8,
        keep_last_messages=2,
        keep_last_tool_results=0,
        image_offload=False,
        summarizer=_Summarizer(),
    )
    transcript = [
        LLMMessage.text_of("user", "look"),
        _image_message("img-1"),
        _image_message("img-2"),
        LLMMessage.text_of("assistant", "thinking"),
        LLMMessage.text_of("assistant", "wrapping up"),
    ]

    class _Ctx:
        round_index = 0
        last_prompt_tokens = None
        last_prompt_transcript_len = 0

    action = await hooks.pre_step(list(transcript), _Ctx())
    assert action.rewrite is not None
    out = action.rewrite.messages
    assert not any(
        "[image offloaded" in getattr(p, "text", "") for m in out for p in m.content
    )
