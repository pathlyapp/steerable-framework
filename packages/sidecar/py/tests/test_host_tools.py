"""CoreLoop + toolsViaHost: tool calls route to the host over the reverse channel."""

from __future__ import annotations

import asyncio
import json

import pytest
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime.llm import LLMStreamChunk, LLMUsage
from steerable_sidecar.host_tools import HostToolExecutor
from steerable_sidecar.sidecar import Sidecar


class _ScriptedProvider:
    name = "scripted"
    model = "scripted-model"

    def __init__(self, script: list[list[LLMStreamChunk]]):
        self._script = script
        self._round = 0

    async def complete(self, *args, **kwargs):
        raise NotImplementedError

    def stream(self, messages, **kwargs):
        chunks = self._script[min(self._round, len(self._script) - 1)]
        self._round += 1

        async def _gen():
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
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit_notification(self, method: str, params: dict | None = None) -> None:
        self.events.append((method, params or {}))

    async def aclose(self) -> None:
        return None


class _HostWriter:
    """Pretends to be the Electron host: captures outbound frames and answers
    reverse ``tool.invoke`` requests from a scripted result table."""

    def __init__(self, server, results: dict[str, dict]):
        self._server = server
        self._results = results
        self.reverse_calls: list[dict] = []

    def write(self, data: bytes):
        payload = json.loads(data)
        # Reverse request (sidecar -> host): string srv_ id + method.
        if isinstance(payload.get("id"), str) and payload.get("method") == "tool.invoke":
            self.reverse_calls.append(payload["params"])
            # No entry for the tool = host has no handler → never responds.
            if payload["params"]["name"] not in self._results:
                return len(data)

            async def _respond() -> None:
                result = self._results[payload["params"]["name"]]
                await self._server.handle_frame(
                    json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result})
                )

            asyncio.ensure_future(_respond())
        return len(data)

    async def drain(self) -> None:
        return None


def _frame(method: str, params: dict | None = None) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


@pytest.mark.asyncio
async def test_coreloop_tools_via_host_round_trip() -> None:
    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="c1", name="local_exec_shell", arguments={"command": "ls"})),
            _text_round("listed"),
        ]
    )
    sidecar = Sidecar(llm_provider_factory=lambda _params: provider)
    sidecar._transport = _CapturingTransport()  # type: ignore[attr-defined]
    host = _HostWriter(
        sidecar.server,
        {"local_exec_shell": {"success": True, "data": {"value": "file.txt"}}},
    )
    sidecar.server.attach_writer(host)

    response = await sidecar.server.handle_frame(
        _frame(
            "agent.chat.stream",
            {
                "provider": "openai_compat",
                "model": "fake",
                "messages": [{"role": "user", "content": "list files"}],
                "useCoreLoop": True,
                "toolsViaHost": True,
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "local_exec_shell", "parameters": {}},
                    }
                ],
            },
        )
    )
    assert "error" not in response, response
    stream_id = response["result"]["streamId"]
    task = sidecar._streams.get(stream_id)
    if task is not None:
        await task

    # the tool call went to the host, not the (empty) local registry
    assert host.reverse_calls == [
        {
            "id": "c1",
            "name": "local_exec_shell",
            "arguments": {"command": "ls"},
            "context": None,
        }
    ]
    events = sidecar._transport.events  # type: ignore[attr-defined]
    results = [p for m, p in events if m == "stream.chunk" and "toolResult" in p]
    assert len(results) == 1
    assert results[0]["toolResult"]["success"] is True
    done = [p for m, p in events if m == "stream.done"]
    assert done[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_coreloop_tools_via_host_failure_feeds_back() -> None:
    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="c1", name="boom", arguments={})),
            _text_round("it failed"),
        ]
    )
    sidecar = Sidecar(llm_provider_factory=lambda _params: provider)
    sidecar._transport = _CapturingTransport()  # type: ignore[attr-defined]
    host = _HostWriter(
        sidecar.server,
        {"boom": {"success": False, "error": "host exploded", "needsFollowup": True}},
    )
    sidecar.server.attach_writer(host)

    response = await sidecar.server.handle_frame(
        _frame(
            "agent.chat.stream",
            {
                "provider": "openai_compat",
                "model": "fake",
                "messages": [{"role": "user", "content": "go"}],
                "useCoreLoop": True,
                "toolsViaHost": True,
            },
        )
    )
    stream_id = response["result"]["streamId"]
    task = sidecar._streams.get(stream_id)
    if task is not None:
        await task

    events = sidecar._transport.events  # type: ignore[attr-defined]
    results = [p for m, p in events if m == "stream.chunk" and "toolResult" in p]
    assert results[0]["toolResult"]["success"] is False
    assert results[0]["toolResult"]["error"] == "host exploded"
    done = [p for m, p in events if m == "stream.done"]
    assert done[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_host_executor_unit_unanswered_host_is_tool_error() -> None:
    # If the host never answers (e.g. no handler registered), the reverse
    # call times out into a failed ToolResult rather than hanging the loop.
    from steerable_agent_runtime import LoopContext
    from steerable_agent_runtime.transport.stdio_jsonrpc import JsonRpcServer

    server = JsonRpcServer()
    server.attach_writer(_HostWriter(server, {}))  # never responds to "ghost"
    executor = HostToolExecutor(server, timeout=0.05)

    result = await executor.execute(
        ToolCall(id="c9", name="ghost", arguments={}), LoopContext()
    )
    assert result.success is False
    assert result.needsFollowup is True
