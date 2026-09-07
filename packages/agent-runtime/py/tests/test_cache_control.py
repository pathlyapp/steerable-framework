"""CacheControlProvider: prompt-cache breakpoint emission (Wave 4, W4-4).

Covers the three-anchor placement (system / last tool / transcript tail),
the per-request ``cache_retention="none"`` suppression (compaction's
summarization case), pass-through for providers without a breakpoint API,
and the Anthropic body-builder integration (control keys never leak
upstream; the OpenAI→Anthropic tool shape transform preserves a stamped
breakpoint).
"""

from __future__ import annotations

from typing import Any

import pytest

from steerable_agent_runtime import (
    CacheControlProvider,
    LLMMessage,
    place_cache_breakpoints,
    system_blocks_with_cache,
)
from steerable_agent_runtime.llm.anthropic_native import (
    AnthropicProvider,
    _openai_tool_to_anthropic,
)


class _RecordingInner:
    """LLMProvider test double capturing the kwargs it is called with."""

    name = "anthropic"
    model = "claude-test"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages, *, tools=None, **kw):  # type: ignore[no-untyped-def]
        self.calls.append({"messages": messages, "tools": tools, **kw})
        return LLMMessage.text_of("assistant", "ok"), None

    def stream(self, messages, *, tools=None, **kw):  # type: ignore[no-untyped-def]
        self.calls.append({"messages": messages, "tools": tools, **kw})

        async def _gen():  # pragma: no cover - trivial
            return
            yield

        return _gen()


def _tools() -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": "a", "parameters": {}}},
        {"type": "function", "function": {"name": "b", "parameters": {}}},
    ]


def test_place_cache_breakpoints_stamps_last_tool_only() -> None:
    out = place_cache_breakpoints(_tools())
    assert "cache_control" not in out[0]
    assert out[1]["cache_control"] == {"type": "ephemeral"}
    # Input descriptors are never mutated.
    assert all("cache_control" not in t for t in _tools())


def test_place_cache_breakpoints_empty_list() -> None:
    assert place_cache_breakpoints([]) == []


def test_system_blocks_with_cache_carries_breakpoint() -> None:
    blocks = system_blocks_with_cache("you are helpful")
    assert blocks == [
        {"type": "text", "text": "you are helpful", "cache_control": {"type": "ephemeral"}}
    ]


@pytest.mark.asyncio
async def test_wrapper_stamps_tools_and_tail_anchor() -> None:
    inner = _RecordingInner()
    provider = CacheControlProvider(inner)
    await provider.complete(
        [LLMMessage.text_of("user", "hi")],
        tools=_tools(),
    )
    call = inner.calls[0]
    assert call["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert call["_cache_tail_anchor"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_wrapper_per_request_retention_none_suppresses_all_anchors() -> None:
    inner = _RecordingInner()
    provider = CacheControlProvider(inner)
    await provider.complete(
        [LLMMessage.text_of("user", "summarize")],
        tools=_tools(),
        cache_retention="none",
    )
    call = inner.calls[0]
    assert all("cache_control" not in t for t in call["tools"])
    assert "_cache_tail_anchor" not in call
    # The control key itself never reaches the inner provider.
    assert "cache_retention" not in call


@pytest.mark.asyncio
async def test_wrapper_passes_through_non_anthropic_providers() -> None:
    inner = _RecordingInner()
    inner.name = "ollama"  # implicit prefix cache — no breakpoint surface
    provider = CacheControlProvider(inner)
    await provider.complete([LLMMessage.text_of("user", "hi")], tools=_tools())
    call = inner.calls[0]
    assert all("cache_control" not in t for t in call["tools"])
    assert "_cache_tail_anchor" not in call
    assert "cache_retention" not in call


@pytest.mark.asyncio
async def test_wrapper_stream_path_applies_the_same_shaping() -> None:
    inner = _RecordingInner()
    provider = CacheControlProvider(inner)
    stream = provider.stream([LLMMessage.text_of("user", "hi")], tools=_tools())
    async for _ in stream:
        pass
    call = inner.calls[0]
    assert call["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert call["_cache_tail_anchor"] == {"type": "ephemeral"}


def test_openai_to_anthropic_transform_preserves_a_stamped_breakpoint() -> None:
    stamped = place_cache_breakpoints(_tools())[-1]
    converted = _openai_tool_to_anthropic(stamped)
    assert converted["name"] == "b"
    assert converted["cache_control"] == {"type": "ephemeral"}


def test_anthropic_body_builder_places_all_three_anchors() -> None:
    provider = AnthropicProvider(name="anthropic", model="claude-test")
    body = provider._build_body(
        messages=[
            LLMMessage.text_of("system", "you are helpful"),
            LLMMessage.text_of("user", "hello"),
        ],
        tools=place_cache_breakpoints(_tools()),
        temperature=None,
        max_tokens=None,
        extra={"_cache_tail_anchor": True},
    )
    # Anchor 1: system prompt as a block array with a breakpoint.
    assert body["system"] == [
        {
            "type": "text",
            "text": "you are helpful",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    # Anchor 2: last tool definition (stamped upstream, preserved here).
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    # Anchor 3: the transcript tail — the last user message lifted to a
    # block carrying the breakpoint.
    tail = body["messages"][-1]
    assert tail["role"] == "user"
    assert tail["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # The control key never leaks into the wire body.
    assert "_cache_tail_anchor" not in body


def test_anthropic_body_builder_without_anchor_keeps_legacy_shapes() -> None:
    # No wrapper → byte-identical legacy bodies (string system, string
    # content, no cache_control anywhere).
    provider = AnthropicProvider(name="anthropic", model="claude-test")
    body = provider._build_body(
        messages=[
            LLMMessage.text_of("system", "you are helpful"),
            LLMMessage.text_of("user", "hello"),
        ],
        tools=_tools(),
        temperature=None,
        max_tokens=None,
        extra={},
    )
    assert body["system"] == "you are helpful"
    assert body["messages"][-1]["content"] == "hello"
    assert all("cache_control" not in t for t in body["tools"])


class TestRetentionTtl:
    """STEERABLE_PROMPT_CACHE_TTL parity with CC's CLAUDE_CODE_PROMPT_CACHE_TTL:
    the retention class maps onto the breakpoint marker's TTL, and all three
    anchors of one request share it."""

    def test_short_retention_is_the_5m_default(self) -> None:
        inner = _RecordingInner()
        provider = CacheControlProvider(inner, retention="short")
        tools, kwargs = provider._apply([{"type": "function"}], {})
        assert kwargs["_cache_tail_anchor"] == {"type": "ephemeral"}
        assert tools[-1]["cache_control"] == {"type": "ephemeral"}

    def test_long_retention_opts_into_the_1h_ttl(self) -> None:
        inner = _RecordingInner()
        provider = CacheControlProvider(inner, retention="long")
        tools, kwargs = provider._apply([{"type": "function"}], {})
        assert kwargs["_cache_tail_anchor"] == {"type": "ephemeral", "ttl": "1h"}
        assert tools[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    def test_per_request_retention_overrides_the_default(self) -> None:
        inner = _RecordingInner()
        provider = CacheControlProvider(inner, retention="long")
        tools, kwargs = provider._apply(
            [{"type": "function"}], {"cache_retention": "short"}
        )
        assert kwargs["_cache_tail_anchor"] == {"type": "ephemeral"}
        assert tools[-1]["cache_control"] == {"type": "ephemeral"}

    def test_anthropic_body_carries_the_ttl_on_all_three_anchors(self) -> None:
        provider = AnthropicProvider(name="anthropic", model="claude-test")
        body = provider._build_body(
            messages=[
                LLMMessage.text_of("system", "sys"),
                LLMMessage.text_of("user", "hi"),
            ],
            tools=[{"type": "function", "function": {"name": "t"}, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            temperature=None,
            max_tokens=None,
            extra={"_cache_tail_anchor": {"type": "ephemeral", "ttl": "1h"}},
        )
        assert body["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        assert body["messages"][-1]["content"][-1]["cache_control"] == {
            "type": "ephemeral",
            "ttl": "1h",
        }
        assert body["tools"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        assert "_cache_tail_anchor" not in body


class TestCacheDriftMonitor:
    """The deliberate loop logic over the raw stage_complete cache numbers."""

    class _Ctx:
        def __init__(self, prompt: int | None, cached: int | None) -> None:
            self.last_prompt_tokens = prompt
            self.last_cached_prompt_tokens = cached

    @pytest.mark.asyncio
    async def test_never_arms_without_cache_accounting(self) -> None:
        from steerable_agent_runtime import CacheDriftMonitor

        monitor = CacheDriftMonitor(consecutive_rounds=2)
        for _ in range(5):
            await monitor.pre_step([], self._Ctx(10_000, 0))
        assert monitor.armed is False
        assert monitor.drift_detected is False

    @pytest.mark.asyncio
    async def test_arms_on_first_hit_and_flags_a_sustained_collapse(self) -> None:
        from steerable_agent_runtime import CacheDriftMonitor

        monitor = CacheDriftMonitor(min_prompt_tokens=1000, min_hit_rate=0.5, consecutive_rounds=3)
        await monitor.pre_step([], self._Ctx(10_000, 9_000))  # cache working → armed
        assert monitor.armed is True
        for round_index in range(3):
            await monitor.pre_step([], self._Ctx(10_000, 1_000))  # 10% hit rate
            assert monitor.consecutive_low_hit_rounds == round_index + 1
        assert monitor.drift_detected is True
        assert monitor.last_hit_rate == 0.1

    @pytest.mark.asyncio
    async def test_one_low_round_is_not_drift_and_recovery_clears(self) -> None:
        from steerable_agent_runtime import CacheDriftMonitor

        monitor = CacheDriftMonitor(min_prompt_tokens=1000, min_hit_rate=0.5, consecutive_rounds=3)
        await monitor.pre_step([], self._Ctx(10_000, 9_000))
        await monitor.pre_step([], self._Ctx(10_000, 100))  # post-compaction re-warm round
        await monitor.pre_step([], self._Ctx(10_000, 9_500))  # recovered
        assert monitor.consecutive_low_hit_rounds == 0
        assert monitor.drift_detected is False

    @pytest.mark.asyncio
    async def test_small_prompts_are_ignored(self) -> None:
        from steerable_agent_runtime import CacheDriftMonitor

        monitor = CacheDriftMonitor(min_prompt_tokens=1024, consecutive_rounds=1)
        await monitor.pre_step([], self._Ctx(10_000, 9_000))  # arm
        await monitor.pre_step([], self._Ctx(500, 0))  # below min_prompt_tokens
        assert monitor.drift_detected is False
