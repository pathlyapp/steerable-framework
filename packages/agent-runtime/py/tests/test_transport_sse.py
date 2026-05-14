from __future__ import annotations

import asyncio

import pytest

from steerable_agent_protocol.generated import SSEEvent
from steerable_agent_runtime.transport import FastAPISseTransport
from steerable_agent_runtime.transport.fastapi_sse import (
    decode_sse_event,
    encode_sse_event,
)


def test_encode_sse_event_basic_payload() -> None:
    event = SSEEvent(type="content", content="hello")
    encoded = encode_sse_event(event)
    assert encoded.endswith("\n\n")
    assert "data:" in encoded
    assert "\"content\":\"hello\"" in encoded


def test_encode_decode_round_trip() -> None:
    original = SSEEvent(type="tool_call", payload={"name": "list_events"})
    decoded = decode_sse_event(encode_sse_event(original))
    assert decoded is not None
    assert decoded.type == "tool_call"
    assert decoded.payload == {"name": "list_events"}


def test_decode_handles_empty_payload() -> None:
    assert decode_sse_event(":\n\n") is None


@pytest.mark.asyncio
async def test_transport_emit_then_close_drains_queue() -> None:
    transport = FastAPISseTransport()
    await transport.emit(SSEEvent(type="content", content="part-1"))
    await transport.emit(SSEEvent(type="content", content="part-2"))
    await transport.aclose()

    received: list[SSEEvent] = []
    async for event in transport.stream():
        received.append(event)
    assert [e.content for e in received] == ["part-1", "part-2"]


@pytest.mark.asyncio
async def test_transport_emit_after_close_raises() -> None:
    from steerable_agent_runtime.errors import TransportError

    transport = FastAPISseTransport()
    await transport.aclose()
    with pytest.raises(TransportError):
        await transport.emit(SSEEvent(type="content", content="x"))


@pytest.mark.asyncio
async def test_transport_stream_yields_concurrently() -> None:
    transport = FastAPISseTransport(queue_size=4)

    async def producer() -> None:
        for i in range(3):
            await transport.emit(SSEEvent(type="content", content=str(i)))
            await asyncio.sleep(0)
        await transport.aclose()

    received: list[str] = []

    async def consumer() -> None:
        async for event in transport.stream():
            received.append(event.content or "")

    await asyncio.gather(producer(), consumer())
    assert received == ["0", "1", "2"]
