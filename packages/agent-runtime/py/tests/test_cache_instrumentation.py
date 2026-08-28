"""Wave 2 cache instrumentation: provider usage parsing → loop telemetry.

The cached-token fields on ``LLMUsage`` are parsed from each provider's
native accounting (OpenAI ``prompt_tokens_details.cached_tokens``, DeepSeek
``prompt_cache_hit_tokens``, Anthropic ``cache_read_input_tokens`` /
``cache_creation_input_tokens``) and surfaced on the loop's
``stage_complete`` event, so ``TraceRecorder`` persists them with no new
plumbing. The hit ratio ``cachedPromptTokens / promptTokens`` is the
observable that world-state diffing (the next Wave 2 item) has to move.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from steerable_agent_runtime import (
    CoreLoop,
    LLMMessage,
    LLMStreamChunk,
    LLMUsage,
    LoopEvent,
    RouterToolExecutor,
    ToolRouter,
    TraceRecorder,
    tool,
)
from steerable_agent_runtime.storage import InMemoryStorage
from steerable_agent_runtime.llm.anthropic_native import _usage_of
from steerable_agent_runtime.llm.openai_compat import _parse_stream_chunk, _parse_usage
from steerable_agent_protocol.generated import ToolCall
from collections.abc import AsyncIterator


# ---------------------------------------------------------------------------
# OpenAI-compatible usage parsing
# ---------------------------------------------------------------------------


def test_openai_usage_reads_prompt_tokens_details_cached_tokens() -> None:
    usage = _parse_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "total_tokens": 1050,
            "prompt_tokens_details": {"cached_tokens": 800},
        }
    )
    assert usage == LLMUsage(
        prompt_tokens=1000,
        completion_tokens=50,
        total_tokens=1050,
        cached_prompt_tokens=800,
    )


def test_openai_usage_falls_back_to_deepseek_top_level_cache_hit() -> None:
    usage = _parse_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "total_tokens": 1050,
            "prompt_cache_hit_tokens": 640,
            "prompt_cache_miss_tokens": 360,
        }
    )
    assert usage.cached_prompt_tokens == 640


def test_openai_usage_without_cache_accounting_is_zero() -> None:
    usage = _parse_usage(
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    )
    assert usage.cached_prompt_tokens == 0
    assert usage.cache_creation_tokens == 0


def test_openai_stream_usage_chunk_carries_cached_tokens() -> None:
    chunk = _parse_stream_chunk(
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 20,
                "total_tokens": 520,
                "prompt_tokens_details": {"cached_tokens": 320},
            },
        }
    )
    assert chunk is not None
    assert chunk.usage is not None
    assert chunk.usage.cached_prompt_tokens == 320


# ---------------------------------------------------------------------------
# Anthropic usage parsing
# ---------------------------------------------------------------------------


def test_anthropic_usage_reads_cache_read_and_creation() -> None:
    usage = _usage_of(
        SimpleNamespace(
            input_tokens=200,
            output_tokens=40,
            cache_read_input_tokens=1500,
            cache_creation_input_tokens=300,
        )
    )
    assert usage == LLMUsage(
        prompt_tokens=200,
        completion_tokens=40,
        total_tokens=240,
        cached_prompt_tokens=1500,
        cache_creation_tokens=300,
    )


def test_anthropic_usage_without_cache_fields_is_zero() -> None:
    usage = _usage_of(SimpleNamespace(input_tokens=10, output_tokens=5))
    assert usage.cached_prompt_tokens == 0
    assert usage.cache_creation_tokens == 0


# ---------------------------------------------------------------------------
# Loop integration: usage → stage_complete → trace
# ---------------------------------------------------------------------------


def _provider(script: list[dict[str, Any]]):
    class _FakeProvider:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self._idx = 0

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
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop",
                    usage=entry.get("usage"),
                )

            return _gen()

    return _FakeProvider()


async def _collect(events) -> list[LoopEvent]:
    return [event async for event in events]


@pytest.mark.asyncio
async def test_stage_complete_carries_cache_telemetry() -> None:
    provider = _provider(
        [
            {
                "tool_calls": [ToolCall(id="c1", name="echo", arguments={"text": "x"})],
                "usage": LLMUsage(
                    prompt_tokens=1000,
                    completion_tokens=12,
                    total_tokens=1012,
                    cached_prompt_tokens=800,
                ),
            },
            {
                "content": "done",
                "usage": LLMUsage(
                    prompt_tokens=1100,
                    completion_tokens=5,
                    total_tokens=1105,
                    cached_prompt_tokens=1000,
                ),
            },
        ]
    )
    router = ToolRouter()

    @tool(router=router, description="Echo text")
    async def echo(text: str) -> dict[str, str]:
        return {"echo": text}

    loop = CoreLoop(provider, RouterToolExecutor(router))
    events = await _collect(loop.run([LLMMessage.text_of("user", "go")]))

    stages = [e.data for e in events if e.kind == "stage_complete"]
    assert len(stages) == 1
    assert stages[0]["promptTokens"] == 1000
    assert stages[0]["cachedPromptTokens"] == 800
    assert stages[0]["cacheCreationTokens"] == 0


@pytest.mark.asyncio
async def test_stage_complete_without_cache_accounting_reports_zero() -> None:
    provider = _provider(
        [
            {
                "tool_calls": [ToolCall(id="c1", name="echo", arguments={"text": "x"})],
                "usage": LLMUsage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
            },
            {"content": "done"},
        ]
    )
    router = ToolRouter()

    @tool(router=router, description="Echo text")
    async def echo(text: str) -> dict[str, str]:
        return {"echo": text}

    loop = CoreLoop(provider, RouterToolExecutor(router))
    events = await _collect(loop.run([LLMMessage.text_of("user", "go")]))

    stages = [e.data for e in events if e.kind == "stage_complete"]
    assert len(stages) == 1
    assert stages[0]["promptTokens"] == 10
    assert stages[0]["cachedPromptTokens"] == 0
    assert stages[0]["cacheCreationTokens"] == 0


@pytest.mark.asyncio
async def test_trace_recorder_persists_cache_telemetry() -> None:
    """No new plumbing: the stage_complete payload lands in the trace."""
    provider = _provider(
        [
            {
                "tool_calls": [ToolCall(id="c1", name="echo", arguments={"text": "x"})],
                "usage": LLMUsage(
                    prompt_tokens=900,
                    completion_tokens=8,
                    total_tokens=908,
                    cached_prompt_tokens=512,
                    cache_creation_tokens=128,
                ),
            },
            {"content": "done"},
        ]
    )
    router = ToolRouter()

    @tool(router=router, description="Echo text")
    async def echo(text: str) -> dict[str, str]:
        return {"echo": text}

    storage = InMemoryStorage()
    recorder = TraceRecorder(storage, chat_id="chat_1")
    loop = CoreLoop(provider, RouterToolExecutor(router))
    await _collect(recorder.tee(loop.run([LLMMessage.text_of("user", "go")])))

    events = await storage.list_events(recorder.trace_id)
    stage = next(e for e in events if e.kind == "stage_complete")
    assert stage.payload["cachedPromptTokens"] == 512
    assert stage.payload["cacheCreationTokens"] == 128
    assert stage.payload["promptTokens"] == 900
