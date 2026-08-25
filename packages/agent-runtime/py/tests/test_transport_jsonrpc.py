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


# ---------------------------------------------------------------------------
# Reverse channel (this peer -> other peer)
# ---------------------------------------------------------------------------


def _read_frames(writer: _FakeWriter) -> list[dict]:
    """Parse all complete newline-delimited frames a writer has buffered."""

    text = bytes(writer.buffer).decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_reverse_call_sends_srv_id_and_resolves_on_response() -> None:
    server = JsonRpcServer()
    writer = _FakeWriter()
    server.attach_writer(writer)

    async def make_call():
        return await server.call("tool.invoke", {"name": "local_exec_shell"})

    task = asyncio.ensure_future(make_call())
    await asyncio.sleep(0)  # let the request frame flush

    frames = _read_frames(writer)
    assert len(frames) == 1
    request = frames[0]
    assert request["method"] == "tool.invoke"
    assert request["params"] == {"name": "local_exec_shell"}
    assert isinstance(request["id"], str)
    assert request["id"].startswith("srv_")

    # The peer replies with a response frame (id, no method).
    await server.handle_frame(
        json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"success": True}})
    )
    assert await task == {"success": True}


@pytest.mark.asyncio
async def test_reverse_call_surfaces_peer_error() -> None:
    server = JsonRpcServer()
    writer = _FakeWriter()
    server.attach_writer(writer)

    task = asyncio.ensure_future(server.call("tool.invoke", {"name": "x"}))
    await asyncio.sleep(0)
    request_id = _read_frames(writer)[0]["id"]

    await server.handle_frame(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32030, "message": "tool blew up", "kind": "tool_failed"},
            }
        )
    )
    with pytest.raises(JsonRpcError) as excinfo:
        await task
    assert excinfo.value.code == -32030
    assert excinfo.value.kind == "tool_failed"


@pytest.mark.asyncio
async def test_reverse_call_requires_attached_writer() -> None:
    server = JsonRpcServer()
    with pytest.raises(RuntimeError):
        await server.call("tool.invoke", {})


@pytest.mark.asyncio
async def test_reverse_call_times_out() -> None:
    server = JsonRpcServer()
    server.attach_writer(_FakeWriter())
    with pytest.raises(asyncio.TimeoutError):
        await server.call("tool.invoke", {}, timeout=0.05)


@pytest.mark.asyncio
async def test_stray_string_id_response_is_ignored() -> None:
    server = JsonRpcServer()
    server.attach_writer(_FakeWriter())
    # A response for an id we never issued must not error or be dispatched.
    out = await server.handle_frame(
        json.dumps({"jsonrpc": "2.0", "id": "srv_999", "result": None})
    )
    assert out is None
