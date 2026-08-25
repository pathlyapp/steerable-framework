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
    small = [LLMMessage(role="user", content="hi")]
    big = [LLMMessage(role="user", content="x" * 40_000)]
    assert estimate_tokens(big) > estimate_tokens(small)


@pytest.mark.asyncio
async def test_under_threshold_transcript_unchanged() -> None:
    provider = make_provider([{"content": "answer"}])
    router = ToolRouter()
    hooks = CompactionHooks(max_context_tokens=60_000, threshold_ratio=0.8)
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)

    await collect(loop.run([LLMMessage(role="user", content="2+2?")]))
    assert hooks.compactions == 0
    assert provider.calls[0][0].content == "2+2?"


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
    events = await collect(loop.run([LLMMessage(role="user", content="go")]))

    assert hooks.compactions >= 1
    # some later model call saw folded tool output instead of the raw blob
    folded_seen = any(
        "[tool output folded" in (m.content or "")
        for call in provider.calls[1:]
        for m in call
        if m.role == "tool"
    )
    assert folded_seen
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
    events = await collect(loop.run([LLMMessage(role="user", content="go")]))

    assert hooks.compactions >= 1
    summarized_seen = any(
        "[context compacted" in (m.content or "")
        for call in provider.calls[1:]
        for m in call
    )
    assert summarized_seen
    assert events[-1].data["status"] == "completed"


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
                LLMMessage(role="system", content="you are helpful"),
                LLMMessage(role="user", content="the original goal"),
            ]
        )
    )

    # every call after compaction still starts with system + original goal
    for call in provider.calls[1:]:
        assert call[0].role == "system" and call[0].content == "you are helpful"
        assert call[1].role == "user" and call[1].content == "the original goal"
