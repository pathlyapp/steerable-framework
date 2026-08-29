"""W6-9: per-turn usage accumulation (loop) + optional cost estimation (pricing)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_runtime import (
    CoreLoop,
    ModelPrice,
    RouterToolExecutor,
    ToolRouter,
    estimate_cost_usd,
    price_for_model,
    register_model_price,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk, LLMUsage
from steerable_agent_runtime.pricing import _custom_prices

from test_trace_recorder import tc


def make_usage_provider(script: list[dict[str, Any]], model: str = "fake-model"):
    """A provider that emits a usage chunk per request (unlike the shared
    make_provider, which reports none)."""

    class _UsageProvider:
        name = "fake"
        def __init__(self) -> None:
            self._idx = 0
        # model is a class-level attr on the shared fake; here it's per-instance.
        @property
        def model(self) -> str:  # noqa: D102
            return model

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            entry = script[min(self._idx, len(script) - 1)]
            self._idx += 1

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                if entry.get("content"):
                    yield LLMStreamChunk(content_delta=entry["content"])
                for call in entry.get("tool_calls", []):
                    yield LLMStreamChunk(tool_call_delta=call)
                yield LLMStreamChunk(usage=entry.get("usage"))
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop"
                )

            return _gen()

    return _UsageProvider()


@pytest.mark.asyncio
async def test_loop_accumulates_billable_usage_across_requests() -> None:
    router = ToolRouter()

    async def add(a: int, b: int) -> int:
        return a + b

    router.register(add)
    # Two requests: round 0 (tool call) + round 1 (final answer). Each bills
    # its own prompt+completion; the run total must SUM them (not take last).
    provider = make_usage_provider(
        [
            {
                "content": "",
                "tool_calls": [tc("add", {"a": 1, "b": 2})],
                "usage": LLMUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
            },
            {
                "content": "sum is 3",
                "usage": LLMUsage(prompt_tokens=150, completion_tokens=10, total_tokens=160),
            },
        ]
    )
    loop = CoreLoop(provider, RouterToolExecutor(router))
    events = []
    async for event in loop.run([LLMMessage.text_of("user", "go")]):
        events.append(event)

    usage = loop.last_run_usage
    assert usage is not None
    assert usage.prompt_tokens == 250  # 100 + 150
    assert usage.completion_tokens == 30  # 20 + 10
    assert usage.total_tokens == 280

    # The terminal completion event carries the run's final totals.
    completion = [e for e in events if e.kind == "completion"][-1]
    assert completion.data["usage"]["promptTokens"] == 250
    assert completion.data["usage"]["completionTokens"] == 30
    assert completion.data["usage"]["totalTokens"] == 280


@pytest.mark.asyncio
async def test_last_run_usage_none_before_first_run() -> None:
    loop = CoreLoop(make_usage_provider([{"content": "x"}]), RouterToolExecutor(ToolRouter()))
    assert loop.last_run_usage is None


def test_price_for_model_longest_prefix() -> None:
    assert price_for_model("deepseek-reasoner").input_per_mtok == 0.55
    assert price_for_model("deepseek-chat").input_per_mtok == 0.27
    assert price_for_model("DeepSeek-V4").output_per_mtok == 1.10  # case-insensitive
    # local/self-hosted models are unpriced
    assert price_for_model("llama3.1:8b") is None
    assert price_for_model(None) is None


def test_estimate_cost_usd() -> None:
    # deepseek-chat: $0.27/1M in, $1.10/1M out.
    cost = estimate_cost_usd("deepseek-chat", 1_000_000, 1_000_000)
    assert cost == pytest.approx(0.27 + 1.10)
    # Unpriced model → None (never a fabricated $0.00).
    assert estimate_cost_usd("llama3.1:8b", 1000, 1000) is None
    assert estimate_cost_usd(None, 1000, 1000) is None


def test_register_model_price_override() -> None:
    snapshot = list(_custom_prices)
    try:
        register_model_price(ModelPrice("deepseek", 1.00, 2.00))
        assert price_for_model("deepseek-chat").input_per_mtok == 1.00
        assert estimate_cost_usd("deepseek-chat", 1_000_000, 0) == pytest.approx(1.00)
    finally:
        _custom_prices.clear()
        _custom_prices.extend(snapshot)


def test_register_model_price_rejects_empty_pattern() -> None:
    with pytest.raises(ValueError):
        register_model_price(ModelPrice("", 1.0, 1.0))
