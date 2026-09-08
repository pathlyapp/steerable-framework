"""Per-turn MCP mounting on the sidecar's chat.stream path.

The ``mcp`` param spawns one ``McpStdioClient`` per entry, registers its
catalog on the turn's router under the ``mcp__<server>__<tool>`` prefix, and
closes every client when the stream ends. These tests run the production
``_run_chat_stream_coreloop`` path in-process (scripted provider, capturing
transport) against a real fake stdio MCP server subprocess, so the full
chain — spawn → handshake → catalog registration → loop dispatch → teardown
— is exercised without a network or a real LLM.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime.llm import LLMStreamChunk, LLMUsage

from steerable_sidecar.sidecar import Sidecar


# A minimal NDJSON JSON-RPC stdio MCP server: initialize → tools/list →
# tools/call(echo). Kept self-contained so the test owns its fixture.
_FAKE_SERVER = textwrap.dedent(
    """
    import json
    import sys

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
            send({
                "jsonrpc": "2.0",
                "id": msg["id"],
                "result": {"tools": TOOLS},
            })
        elif method == "tools/call":
            args = msg["params"].get("arguments") or {}
            send({
                "jsonrpc": "2.0",
                "id": msg["id"],
                "result": {
                    "content": [
                        {"type": "text", "text": "echo:" + args.get("text", "")},
                    ],
                },
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


class _ScriptedProvider:
    """Plays a fixed script of rounds."""

    name = "scripted"
    model = "scripted-model"

    def __init__(self, script: list[list[LLMStreamChunk]]):
        self._script = script
        self._round = 0
        self.stream_kwargs: list[dict] = []

    async def complete(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def stream(self, messages: Any, **kwargs: Any) -> Any:
        self.stream_kwargs.append(dict(kwargs))
        chunks = self._script[min(self._round, len(self._script) - 1)]
        self._round += 1

        async def _gen() -> Any:
            for chunk in chunks:
                yield chunk

        return _gen()


def _text_round(text: str) -> list[LLMStreamChunk]:
    return [
        LLMStreamChunk(content_delta=text),
        LLMStreamChunk(
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=5, completion_tokens=1, total_tokens=6),
        ),
    ]


def _tool_round(call: ToolCall) -> list[LLMStreamChunk]:
    return [
        LLMStreamChunk(tool_call_delta=call),
        LLMStreamChunk(
            finish_reason="tool_calls",
            usage=LLMUsage(prompt_tokens=5, completion_tokens=1, total_tokens=6),
        ),
    ]


class _CapturingTransport:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit_notification(
        self, method: str, params: dict | None = None
    ) -> None:
        self.events.append((method, params or {}))

    async def aclose(self) -> None:
        return None


def _make_sidecar(provider: _ScriptedProvider) -> Sidecar:
    sidecar = Sidecar(llm_provider_factory=lambda _params: provider)
    sidecar._transport = _CapturingTransport()  # type: ignore[attr-defined]
    return sidecar


async def _run_stream(
    sidecar: Sidecar, params: dict
) -> tuple[str, list[tuple[str, dict]]]:
    response = await sidecar.server.handle_frame(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "agent.chat.stream", "params": params})
    )
    assert "error" not in response, response
    stream_id = response["result"]["streamId"]
    task = sidecar._streams.get(stream_id)
    if task is not None:
        await task
    return stream_id, sidecar._transport.events  # type: ignore[attr-defined]


def _mcp_params(server: Path) -> dict[str, Any]:
    return {
        "provider": "openai_compat",
        "model": "fake",
        "messages": [{"role": "user", "content": "echo something"}],
        "useCoreLoop": True,
        "mcp": [{"name": "fake", "command": sys.executable, "args": [str(server)]}],
    }


@pytest.mark.asyncio
async def test_mcp_tool_registered_and_dispatched(fake_server: Path) -> None:
    """The catalog lands on the turn's router and the loop dispatches the
    qualified MCP tool for real, its result reaching the model."""
    provider = _ScriptedProvider(
        [
            _tool_round(
                ToolCall(id="e1", name="mcp__fake__echo", arguments={"text": "hi"})
            ),
            _text_round("done"),
        ]
    )
    sidecar = _make_sidecar(provider)

    _stream_id, events = await _run_stream(sidecar, _mcp_params(fake_server))

    # The qualified tool dispatched: its result reached the model as a
    # toolResult chunk carrying the server's echo payload.
    tool_results = [
        p["toolResult"]
        for m, p in events
        if m == "stream.chunk" and "toolResult" in p
    ]
    assert len(tool_results) == 1
    assert tool_results[0]["name"] == "mcp__fake__echo"
    assert tool_results[0]["success"] is True
    assert "echo:hi" in json.dumps(tool_results[0])
    done = [p for m, p in events if m == "stream.done"]
    assert done[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_mcp_client_closed_after_stream(fake_server: Path) -> None:
    """Teardown: the per-turn client is aclosed in the stream's finally, so
    no server subprocess outlives the turn."""
    provider = _ScriptedProvider([_text_round("no tools")])
    sidecar = _make_sidecar(provider)

    await _run_stream(sidecar, _mcp_params(fake_server))

    # The stream ended; the sidecar must not still hold a live client for it.
    # (The subprocess exits once its stdin closes on aclose.)
    assert sidecar._streams == {}
    assert sidecar._coreloops == {}


@pytest.mark.asyncio
async def test_mcp_ignored_under_tools_via_host(fake_server: Path) -> None:
    """Coexistence with the desktop: under toolsViaHost the host owns MCP
    execution (its own mcp__<serverKey>__<tool> dynamic tools dispatch over
    the reverse channel), so the sidecar must NOT also mount the param's
    servers locally — doing so would double-register the mcp__ prefix. The
    turn runs with no sidecar-local MCP registration."""
    provider = _ScriptedProvider([_text_round("host owns tools")])
    sidecar = _make_sidecar(provider)

    params = _mcp_params(fake_server)
    params["toolsViaHost"] = True
    _stream_id, events = await _run_stream(sidecar, params)

    # No mcp__ tool was registered on the sidecar's router for this turn.
    assert sidecar.tools.get("mcp__fake__echo") is None
    done = [p for m, p in events if m == "stream.done"]
    assert done[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_mcp_entry_without_command_fails_loud(fake_server: Path) -> None:
    """A malformed mcp entry is rejected before any provider request."""
    provider = _ScriptedProvider([_text_round("unreachable")])
    sidecar = _make_sidecar(provider)

    params = _mcp_params(fake_server)
    params["mcp"] = [{"name": "broken"}]  # no command
    response = await sidecar.server.handle_frame(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "agent.chat.stream", "params": params})
    )
    # Validation happens in the foreground handler, so the request fails with
    # a clean invalid_params error and the provider is never reached.
    assert "error" in response
    assert "command" in response["error"]["message"]
    assert provider.stream_kwargs == []
