"""Wave 2 step 4: MCP seam — qualified names, capped catalogs, stdio client.

Unit tests cover qualification/parsing and catalog registration invariants
(cap, atomicity, exposure default). Client tests spawn a real fake MCP
server subprocess (NDJSON JSON-RPC over stdio) and exercise the handshake,
cursor pagination, tool calls, server-initiated requests, and timeouts.
The end-to-end test runs the full chain: server → catalog → deferred tier
→ tool_search discovery → loop dispatch.
"""

from __future__ import annotations

import sys
import textwrap
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall

from steerable_agent_runtime import (
    CoreLoop,
    LLMMessage,
    McpCatalogError,
    McpError,
    McpStdioClient,
    McpToolInfo,
    RouterToolExecutor,
    ToolRouter,
    mcp_invoker,
    parse_mcp_name,
    qualify_mcp_name,
    register_mcp_catalog,
    register_tool_search,
    tool,
)
from steerable_agent_runtime.llm import LLMStreamChunk, LLMUsage


def _msg(role: str, text: str) -> LLMMessage:
    return LLMMessage.text_of(role, text)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Name qualification
# ---------------------------------------------------------------------------


def test_qualify_and_parse_round_trip() -> None:
    qualified = qualify_mcp_name("github", "create_issue")
    assert qualified == "mcp__github__create_issue"
    assert parse_mcp_name(qualified) == ("github", "create_issue")


def test_parse_tolerates_separator_in_tool_name() -> None:
    assert parse_mcp_name("mcp__github__create__issue") == ("github", "create__issue")


def test_parse_rejects_non_mcp_names() -> None:
    assert parse_mcp_name("echo") is None
    assert parse_mcp_name("mcp__noserver") is None
    assert parse_mcp_name("other__github__tool") is None


def test_qualify_rejects_separator_in_server_name() -> None:
    with pytest.raises(McpCatalogError):
        qualify_mcp_name("bad__server", "tool")


def test_qualify_enforces_provider_name_cap() -> None:
    with pytest.raises(McpCatalogError):
        qualify_mcp_name("a" * 40, "t" * 40)


# ---------------------------------------------------------------------------
# Catalog registration
# ---------------------------------------------------------------------------


async def _ok_invoker(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "data": {"value": f"{name}:{arguments}"}}


def _catalog(*names: str) -> list[McpToolInfo]:
    return [
        McpToolInfo(
            name=n,
            description=f"{n} description",
            schema={"type": "object", "properties": {"x": {"type": "string"}}},
        )
        for n in names
    ]


def test_catalog_registers_qualified_deferred_by_default() -> None:
    router = ToolRouter()
    registered = register_mcp_catalog(
        router, server="github", tools=_catalog("create_issue"), invoker=_ok_invoker
    )
    assert [r.name for r in registered] == ["mcp__github__create_issue"]
    assert registered[0].exposure == "deferred"
    assert registered[0].metadata == {
        "mcp_server": "github",
        "mcp_tool": "create_issue",
    }
    # Deferred: registered but not model-listed; discoverable via search.
    assert router.describe_model() == []
    assert [t.name for t in router.deferred_tools()] == ["mcp__github__create_issue"]


def test_catalog_cap_fails_loud_and_atomic() -> None:
    router = ToolRouter()
    with pytest.raises(McpCatalogError, match="over the per-server cap"):
        register_mcp_catalog(
            router, server="huge", tools=_catalog("a", "b", "c"), invoker=_ok_invoker,
            max_tools=2,
        )
    # Nothing half-registered.
    assert router.list_tools() == []


def test_catalog_invalid_name_fails_before_any_registration() -> None:
    router = ToolRouter()
    tools = [*_catalog("fine"), McpToolInfo(name="")]
    with pytest.raises(McpCatalogError):
        register_mcp_catalog(router, server="s", tools=tools, invoker=_ok_invoker)
    assert router.list_tools() == []


@pytest.mark.asyncio
async def test_catalog_dispatch_delegates_with_unqualified_name() -> None:
    router = ToolRouter()
    seen: list[tuple[str, dict[str, Any]]] = []

    async def invoker(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        seen.append((name, arguments))
        return {"success": True, "data": {"value": "ok"}}

    register_mcp_catalog(
        router, server="github", tools=_catalog("create_issue"), invoker=invoker
    )
    result = await router.dispatch(
        ToolCall(
            id="1",
            name="mcp__github__create_issue",
            arguments={"x": "hello"},
        )
    )
    assert result.success
    # The invoker sees the *unqualified* server-local name.
    assert seen == [("create_issue", {"x": "hello"})]


# ---------------------------------------------------------------------------
# Fake MCP server subprocess
# ---------------------------------------------------------------------------

_FAKE_SERVER = textwrap.dedent(
    """
    import json
    import sys

    state = {"sampling_rejected": False}

    TOOLS = [
        {
            "name": "echo",
            "description": "Echo text back",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
        {
            "name": "fail",
            "description": "Always returns an error result",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "trigger_sampling",
            "description": "Make the server send a sampling request",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "check_sampling",
            "description": "Report whether the sampling request was rejected",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "never_respond",
            "description": "Never answers (timeout test)",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\\n")
        sys.stdout.flush()


    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if "method" not in msg:
            # A response to a server-initiated request.
            if msg.get("id") == 99 and "error" in msg:
                state["sampling_rejected"] = True
            continue
        method = msg["method"]
        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": msg["id"],
                "result": {
                    "protocolVersion": msg["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-mcp", "version": "1.0"},
                },
            })
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            cursor = (msg.get("params") or {}).get("cursor")
            if cursor is None:
                send({
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {"tools": TOOLS[:3], "nextCursor": "page2"},
                })
            else:
                send({
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {"tools": TOOLS[3:]},
                })
        elif method == "tools/call":
            name = msg["params"]["name"]
            args = msg["params"].get("arguments") or {}
            if name == "echo":
                send({
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {
                        "content": [
                            {"type": "text", "text": "echo:" + args.get("text", "")},
                            {"type": "image", "data": "...", "mimeType": "image/png"},
                        ],
                        "structuredContent": {"echoed": args.get("text", "")},
                    },
                })
            elif name == "fail":
                send({
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {
                        "content": [{"type": "text", "text": "boom"}],
                        "isError": True,
                    },
                })
            elif name == "trigger_sampling":
                # Write the server request *before* the call result: the
                # client's reader answers it before the caller's next
                # request, so check_sampling observes a deterministic state.
                send({
                    "jsonrpc": "2.0",
                    "id": 99,
                    "method": "sampling/createMessage",
                    "params": {},
                })
                send({
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {"content": [{"type": "text", "text": "sent"}]},
                })
            elif name == "check_sampling":
                send({
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "rejected"
                                    if state["sampling_rejected"]
                                    else "missing"
                                ),
                            }
                        ]
                    },
                })
            elif name == "never_respond":
                pass
            else:
                send({
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "error": {"code": -32602, "message": "unknown tool"},
                })
        elif "id" in msg:
            send({
                "jsonrpc": "2.0",
                "id": msg["id"],
                "error": {"code": -32601, "message": "unknown method"},
            })
    """
)


@pytest.fixture()
def fake_server(tmp_path: Path) -> Path:
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(_FAKE_SERVER)
    return script


@pytest.mark.asyncio
async def test_stdio_client_handshake_list_and_call(fake_server: Path) -> None:
    async with McpStdioClient(sys.executable, [str(fake_server)]) as client:
        assert client.server_info == {"name": "fake-mcp", "version": "1.0"}

        tools = await client.list_tools()
        # Cursor pagination stitched both pages.
        assert [t.name for t in tools] == [
            "echo",
            "fail",
            "trigger_sampling",
            "check_sampling",
            "never_respond",
        ]
        assert tools[0].schema["properties"]["text"]["type"] == "string"

        result = await client.call_tool("echo", {"text": "hello"})
        assert not result.is_error
        assert "echo:hello" in result.text
        # Non-text parts surface as placeholders, not silence.
        assert "[mcp image content]" in result.text
        assert result.structured == {"echoed": "hello"}

        failure = await client.call_tool("fail", {})
        assert failure.is_error
        assert "boom" in failure.text


@pytest.mark.asyncio
async def test_stdio_client_answers_server_requests_method_not_found(
    fake_server: Path,
) -> None:
    async with McpStdioClient(sys.executable, [str(fake_server)]) as client:
        await client.call_tool("trigger_sampling", {})
        checked = await client.call_tool("check_sampling", {})
        assert checked.text == "rejected"


@pytest.mark.asyncio
async def test_stdio_client_request_timeout(fake_server: Path) -> None:
    async with McpStdioClient(
        sys.executable, [str(fake_server)], request_timeout=0.2
    ) as client:
        with pytest.raises(McpError, match="timed out"):
            await client.call_tool("never_respond", {})
        # The client stays usable after a timed-out request.
        result = await client.call_tool("echo", {"text": "still alive"})
        assert "echo:still alive" in result.text


@pytest.mark.asyncio
async def test_stdio_client_use_before_start_fails(fake_server: Path) -> None:
    client = McpStdioClient(sys.executable, [str(fake_server)])
    with pytest.raises(McpError, match="not running"):
        await client.call_tool("echo", {})


# ---------------------------------------------------------------------------
# End to end: server → catalog → deferred tier → tool_search → loop
# ---------------------------------------------------------------------------


def _provider(script: list[dict[str, Any]]):
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
                if entry.get("content"):
                    yield LLMStreamChunk(content_delta=entry["content"])
                for call in entry.get("tool_calls", []):
                    yield LLMStreamChunk(tool_call_delta=call)
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop",
                    usage=LLMUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
                )

            return _gen()

    return _FakeProvider()


@pytest.mark.asyncio
async def test_mcp_tool_discovered_and_called_through_the_loop(
    fake_server: Path,
) -> None:
    router = ToolRouter()

    @tool(router=router, description="Local echo tool")
    async def local_ping(text: str) -> str:
        return text

    async with McpStdioClient(sys.executable, [str(fake_server)]) as client:
        catalog = await client.list_tools()
        register_mcp_catalog(
            router, server="fake", tools=catalog, invoker=mcp_invoker(client)
        )
        register_tool_search(router)

        offered = router.describe_model()
        # MCP tools stay off the initial list; only the local tool and the
        # discovery seam are offered.
        assert [d["function"]["name"] for d in offered] == [
            "local_ping",
            "tool_search",
        ]

        provider = _provider(
            [
                {
                    "tool_calls": [
                        ToolCall(
                            id="s1",
                            name="tool_search",
                            arguments={"query": "echo text"},
                        )
                    ]
                },
                {
                    "tool_calls": [
                        ToolCall(
                            id="e1",
                            name="mcp__fake__echo",
                            arguments={"text": "via mcp"},
                        )
                    ]
                },
                {"content": "The MCP server said: echo:via mcp"},
            ]
        )
        loop = CoreLoop(provider, RouterToolExecutor(router))
        events = [
            e
            async for e in loop.run(
                [_msg("user", "echo something via MCP")], tools=offered
            )
        ]

    assert events[-1].kind == "completion"
    tool_messages = [
        m
        for call in provider.calls
        for m in call
        if m.role == "tool" and m.name == "mcp__fake__echo"
    ]
    assert tool_messages, "MCP tool result should reach the model"
    assert "echo:via mcp" in tool_messages[0].content_text
