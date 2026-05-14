from __future__ import annotations

import asyncio
import json

import pytest

from steerable_agent_protocol.generated import SSEEvent
from steerable_agent_runtime.transport.stdio_jsonrpc import (
    JsonRpcError,
    JsonRpcServer,
    StdioJsonRpcTransport,
    decode_frame,
    encode_frame,
)


class _FakeWriter:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)


def test_encode_decode_frame_round_trip() -> None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    encoded = encode_frame(payload)
    assert encoded.endswith(b"\n")
    assert b"\n" not in encoded[:-1], "no embedded newlines"
    decoded = decode_frame(encoded.decode("utf-8"))
    assert decoded == payload


def test_decode_blank_line_returns_none() -> None:
    assert decode_frame("") is None
    assert decode_frame("   \n") is None


@pytest.mark.asyncio
async def test_transport_emits_event_as_notification() -> None:
    writer = _FakeWriter()
    transport = StdioJsonRpcTransport(writer)
    await transport.emit(SSEEvent(type="content", content="hi"))
    payload = json.loads(writer.buffer.decode("utf-8").strip())
    assert payload["jsonrpc"] == "2.0"
    assert payload["method"] == "stream.chunk"
    assert payload["params"]["type"] == "content"
    assert payload["params"]["content"] == "hi"
    assert "id" not in payload  # notifications must not carry id


@pytest.mark.asyncio
async def test_transport_emit_after_close_raises() -> None:
    transport = StdioJsonRpcTransport(_FakeWriter())
    await transport.aclose()
    with pytest.raises(RuntimeError):
        await transport.emit(SSEEvent(type="content", content="x"))


@pytest.mark.asyncio
async def test_server_dispatches_request_and_returns_result() -> None:
    server = JsonRpcServer()

    async def echo(params):
        return {"echoed": params}

    server.register("echo", echo)

    response = await server.handle_frame(
        json.dumps({"jsonrpc": "2.0", "id": 7, "method": "echo", "params": {"x": 1}})
    )
    assert response["id"] == 7
    assert response["result"] == {"echoed": {"x": 1}}
    assert "error" not in response


@pytest.mark.asyncio
async def test_server_returns_method_not_found() -> None:
    server = JsonRpcServer()
    response = await server.handle_frame(
        json.dumps({"jsonrpc": "2.0", "id": "abc", "method": "unknown"})
    )
    assert response["error"]["code"] == -32601
    assert response["error"]["kind"] == "method_not_found"


@pytest.mark.asyncio
async def test_server_handles_parse_error() -> None:
    server = JsonRpcServer()
    response = await server.handle_frame("not-json")
    assert response["error"]["code"] == -32700
    assert response["error"]["kind"] == "parse"


@pytest.mark.asyncio
async def test_server_propagates_jsonrpc_error_kind() -> None:
    server = JsonRpcServer()

    async def boom(_params):
        raise JsonRpcError("budget gone", code=-32010, kind="budget_exhausted", data={"used": 5})

    server.register("boom", boom)
    response = await server.handle_frame(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "boom"})
    )
    err = response["error"]
    assert err["code"] == -32010
    assert err["kind"] == "budget_exhausted"
    assert err["data"] == {"used": 5}


@pytest.mark.asyncio
async def test_server_invalid_request_missing_method() -> None:
    server = JsonRpcServer()
    response = await server.handle_frame(json.dumps({"jsonrpc": "2.0", "id": 1}))
    assert response["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_server_handles_notification_with_handler_invocation() -> None:
    server = JsonRpcServer()
    received: list[dict] = []

    async def on_log(params):
        received.append(params)

    server.register_notification("log.line", on_log)
    out = await server.handle_frame(
        json.dumps({"jsonrpc": "2.0", "method": "log.line", "params": {"level": "INFO"}})
    )
    assert out is None
    await asyncio.sleep(0)  # let handler run if scheduled
    assert received == [{"level": "INFO"}]
