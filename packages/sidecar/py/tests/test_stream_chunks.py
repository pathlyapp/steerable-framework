"""stream_chunks: rawChunk wire projection + the on_stream_chunk bridges."""

from __future__ import annotations

import asyncio

import pytest
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime.llm import LLMStreamChunk, LLMUsage
from steerable_sidecar.stream_chunks import (
    RawChunkBridgeHooks,
    RawChunkStdoutHooks,
    serialize_raw_chunk,
)


def test_serialize_raw_chunk_maps_set_fields_and_drops_raw_and_usage() -> None:
    chunk = LLMStreamChunk(
        content_delta="hi",
        reasoning_delta="think",
        tool_call_delta=ToolCall(id="c1", name="add", arguments={"a": 1}),
        finish_reason="stop",
        usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        raw={"wire": "detail"},
    )
    assert serialize_raw_chunk(chunk) == {
        "contentDelta": "hi",
        "reasoningDelta": "think",
        "toolCallDelta": {"id": "c1", "name": "add", "arguments": {"a": 1}},
        "finishReason": "stop",
    }


def test_serialize_raw_chunk_omits_unset_fields() -> None:
    assert serialize_raw_chunk(LLMStreamChunk()) == {}
    assert serialize_raw_chunk(LLMStreamChunk(content_delta="x")) == {
        "contentDelta": "x"
    }


class _CaptureTransport:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit_notification(self, method: str, params: dict | None = None) -> None:
        self.events.append((method, params or {}))


class _ExplodingTransport:
    async def emit_notification(self, method: str, params: dict | None = None) -> None:
        raise RuntimeError("transport exploded")


@pytest.mark.asyncio
async def test_bridge_emits_stream_chunk_with_raw_chunk() -> None:
    transport = _CaptureTransport()
    hooks = RawChunkBridgeHooks(transport, "s_1")  # type: ignore[arg-type]

    hooks.on_stream_chunk(LLMStreamChunk(content_delta="hi"), None)
    # Emission is fire-and-forget; let the scheduled task run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert transport.events == [
        ("stream.chunk", {"streamId": "s_1", "rawChunk": {"contentDelta": "hi"}})
    ]


@pytest.mark.asyncio
async def test_bridge_skips_empty_chunks_and_never_raises() -> None:
    transport = _ExplodingTransport()
    hooks = RawChunkBridgeHooks(transport, "s_1")  # type: ignore[arg-type]

    # An empty chunk schedules nothing; a failing transport surfaces only as
    # a log record — the loop contract is that the hook never raises.
    hooks.on_stream_chunk(LLMStreamChunk(), None)
    hooks.on_stream_chunk(LLMStreamChunk(content_delta="x"), None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def test_stdout_hook_writes_tagged_line(capsys: pytest.CaptureFixture[str]) -> None:
    hooks = RawChunkStdoutHooks()
    hooks.on_stream_chunk(LLMStreamChunk(content_delta="hi"), None)
    assert '[raw_chunk {"contentDelta": "hi"}]' in capsys.readouterr().out


def test_stdout_hook_never_raises(capsys: pytest.CaptureFixture[str]) -> None:
    hooks = RawChunkStdoutHooks()
    hooks.on_stream_chunk(LLMStreamChunk(), None)  # empty: no output
    assert capsys.readouterr().out == ""
