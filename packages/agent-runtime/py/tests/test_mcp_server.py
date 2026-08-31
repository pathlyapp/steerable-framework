"""W3.2: MCP server — handshake, tools/list, tools/call, elicitation approval."""

from __future__ import annotations

import asyncio
import json

import pytest

from steerable_agent_runtime.mcp_server import McpServer
from steerable_agent_runtime.tools import ToolRouter


class _Wire:
    """In-memory ends of the stdio transport: feed client→server lines,
    collect server→client lines."""

    def __init__(self) -> None:
        self.reader = asyncio.StreamReader()
        self.out = bytearray()

    class _Writer:
        def __init__(self, out: bytearray) -> None:
            self._out = out

        def write(self, data: bytes) -> None:
            self._out.extend(data)

    @property
    def writer(self) -> "_Wire._Writer":
        return _Wire._Writer(self.out)

    def send(self, payload: dict) -> None:
        self.reader.feed_data(json.dumps(payload).encode() + b"\n")

    def take_lines(self) -> list[dict]:
        # Mutate in place: the server's writer holds a reference to this
        # bytearray — rebinding it would orphan every later server write.
        lines = []
        while b"\n" in self.out:
            line, _, _ = self.out.partition(b"\n")
            del self.out[: len(line) + 1]
            lines.append(json.loads(line))
        return lines


async def _rpc(wire: _Wire, msg_id: int, method: str, params: dict | None = None,
               answer_elicitation: dict | None = None) -> dict:
    """Send a request; answer any elicitation/create the server emits with
    ``answer_elicitation``; return the response to ``msg_id``."""
    wire.send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        for line in wire.take_lines():
            if line.get("method") == "elicitation/create":
                wire.send(
                    {
                        "jsonrpc": "2.0",
                        "id": line["id"],
                        "result": answer_elicitation or {"action": "decline"},
                    }
                )
            elif line.get("id") == msg_id:
                return line
        await asyncio.sleep(0.01)
    raise TimeoutError(f"no response to {method}")


def _router() -> ToolRouter:
    router = ToolRouter()

    def read_file(path: str) -> dict:
        """Read a file."""
        return {"path": path, "content": "data"}

    def delete_file(path: str) -> dict:
        """Delete a file."""
        return {"deleted": path}

    router.register(read_file, mode="read")
    router.register(delete_file, mode="destructive")
    return router


@pytest.fixture
async def wire_and_server():
    wire = _Wire()
    server = McpServer(_router())
    task = asyncio.create_task(server.serve(wire.reader, wire.writer))
    yield wire, server
    task.cancel()


async def _initialize(wire: _Wire, *, elicitation: bool) -> dict:
    caps = {"elicitation": {}} if elicitation else {}
    return await _rpc(
        wire, 1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": caps}
    )


@pytest.mark.asyncio
async def test_initialize_negotiates_and_remembers_elicitation(wire_and_server) -> None:
    wire, server = wire_and_server
    response = await _initialize(wire, elicitation=True)
    result = response["result"]
    assert result["serverInfo"]["name"] == "steerable"
    assert "tools" in result["capabilities"]
    assert server._elicitation_supported is True


@pytest.mark.asyncio
async def test_tools_list_projects_router(wire_and_server) -> None:
    wire, _ = wire_and_server
    await _initialize(wire, elicitation=False)
    response = await _rpc(wire, 2, "tools/list")
    names = {t["name"] for t in response["result"]["tools"]}
    assert names == {"read_file", "delete_file"}
    listed = {t["name"]: t for t in response["result"]["tools"]}
    assert listed["read_file"]["inputSchema"]["type"] == "object"


@pytest.mark.asyncio
async def test_read_tool_executes_without_elicitation(wire_and_server) -> None:
    wire, _ = wire_and_server
    await _initialize(wire, elicitation=True)
    response = await _rpc(
        wire, 2, "tools/call", {"name": "read_file", "arguments": {"path": "a.txt"}}
    )
    result = response["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"])["value"]["content"] == "data"


@pytest.mark.asyncio
async def test_destructive_call_elicits_and_executes_on_allow(wire_and_server) -> None:
    wire, _ = wire_and_server
    await _initialize(wire, elicitation=True)
    response = await _rpc(
        wire,
        2,
        "tools/call",
        {"name": "delete_file", "arguments": {"path": "a.txt"}},
        answer_elicitation={"action": "accept", "content": {"decision": "allow_once"}},
    )
    result = response["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"])["value"]["deleted"] == "a.txt"


@pytest.mark.asyncio
async def test_elicitation_decline_blocks_execution(wire_and_server) -> None:
    wire, _ = wire_and_server
    await _initialize(wire, elicitation=True)
    response = await _rpc(
        wire, 2, "tools/call", {"name": "delete_file", "arguments": {"path": "a.txt"}}
    )
    result = response["result"]
    assert result["isError"] is True
    assert "deny_once" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_client_without_elicitation_fails_closed(wire_and_server) -> None:
    wire, _ = wire_and_server
    await _initialize(wire, elicitation=False)
    response = await _rpc(
        wire, 2, "tools/call", {"name": "delete_file", "arguments": {"path": "a.txt"}}
    )
    result = response["result"]
    assert result["isError"] is True
    assert "elicitation" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_allow_for_session_skips_second_prompt(wire_and_server) -> None:
    wire, _ = wire_and_server
    await _initialize(wire, elicitation=True)
    first = await _rpc(
        wire,
        2,
        "tools/call",
        {"name": "delete_file", "arguments": {"path": "a.txt"}},
        answer_elicitation={
            "action": "accept",
            "content": {"decision": "allow_for_session"},
        },
    )
    assert first["result"]["isError"] is False
    # Second call: no elicitation answer wired — if the server elicits again
    # the test times out, proving the cache was consulted.
    second = await _rpc(
        wire, 3, "tools/call", {"name": "delete_file", "arguments": {"path": "b.txt"}}
    )
    assert second["result"]["isError"] is False


@pytest.mark.asyncio
async def test_unknown_method_returns_method_not_found(wire_and_server) -> None:
    wire, _ = wire_and_server
    response = await _rpc(wire, 1, "resources/list")
    assert response["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_notifications_get_no_response(wire_and_server) -> None:
    wire, _ = wire_and_server
    wire.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    await asyncio.sleep(0.1)
    assert wire.take_lines() == []
