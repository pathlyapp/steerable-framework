"""Wave 3: sidecar execSandbox wiring — per-exec confinement of shell calls.

``execSandbox: {enabled: true, ...}`` on chat.stream wraps the tool
executor in ``SandboxedToolExecutor``: shell commands are rewritten to run
under the backend (Seatbelt on macOS) and results carry the
``data._sandbox`` enforcement marker. Absent → legacy unconfined behavior.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest
from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime import ToolRouter
from steerable_agent_runtime.llm import LLMStreamChunk, LLMUsage

from steerable_sidecar.sandbox import seatbelt_available
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


def _frame(method: str, params: dict | None = None) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


async def _run_stream(sidecar: Sidecar, params: dict) -> None:
    response = await sidecar.server.handle_frame(_frame("agent.chat.stream", params))
    assert "error" not in response, response
    task = sidecar._streams.get(response["result"]["streamId"])
    if task is not None:
        await task


def _base_params(**extra) -> dict:
    return {
        "provider": "openai_compat",
        "model": "fake",
        "messages": [{"role": "user", "content": "go"}],
        "useCoreLoop": True,
        "chatId": "chat_1",
        **extra,
    }


def _sidecar_with_bash(received: list[str], provider: _ScriptedProvider) -> Sidecar:
    tools = ToolRouter()

    async def bash(command: str = "") -> ToolResult:
        received.append(command)
        return ToolResult(success=True, data={"stdout": "ok"})

    tools.register(bash, name="bash", mode="other", concurrency_safe=True)
    sidecar = Sidecar(tools=tools, llm_provider_factory=lambda _params: provider)
    sidecar._transport = _CapturingTransport()
    return sidecar


async def _tool_payloads(sidecar: Sidecar) -> list[dict]:
    """Tool-message payloads from the durable record — the full ToolResult
    (including ``data._sandbox``) lives in the transcript, while the wire
    stream only carries a preview."""
    entries = await sidecar.storage.list_history("chat_1")
    return [
        json.loads(entry["message"]["content"][0]["text"])
        for entry in entries
        if entry["kind"] == "tool"
    ]


@pytest.mark.asyncio
async def test_exec_sandbox_rewrites_shell_command() -> None:
    received: list[str] = []
    provider = _ScriptedProvider(
        [_tool_round(ToolCall(id="c1", name="bash", arguments={"command": "ls -la"})),
         _text_round("done")]
    )
    sidecar = _sidecar_with_bash(received, provider)

    await _run_stream(sidecar, _base_params(execSandbox={"enabled": True}))

    assert len(received) == 1
    if seatbelt_available():
        assert "sandbox-exec" in received[0]
        assert "ls -la" in received[0]
        marker = (await _tool_payloads(sidecar))[0]["data"]["_sandbox"]
        assert marker == {"enforcement": "full", "backend": "seatbelt"}
    else:
        # No backend on this platform: command passes through, marked none.
        assert received[0] == "ls -la"
        marker = (await _tool_payloads(sidecar))[0]["data"]["_sandbox"]
        assert marker["enforcement"] == "none"


@pytest.mark.asyncio
async def test_exec_sandbox_absent_keeps_legacy_behavior() -> None:
    received: list[str] = []
    provider = _ScriptedProvider(
        [_tool_round(ToolCall(id="c1", name="bash", arguments={"command": "ls"})),
         _text_round("done")]
    )
    sidecar = _sidecar_with_bash(received, provider)

    await _run_stream(sidecar, _base_params())

    assert received == ["ls"]
    assert "_sandbox" not in (await _tool_payloads(sidecar))[0]["data"]


@pytest.mark.asyncio
async def test_exec_sandbox_leaves_non_shell_tools_alone() -> None:
    received: list[str] = []
    provider = _ScriptedProvider(
        [_tool_round(ToolCall(id="c1", name="read_thing", arguments={"command": "ls"})),
         _text_round("done")]
    )
    tools = ToolRouter()

    async def read_thing(command: str = "") -> ToolResult:
        received.append(command)
        return ToolResult(success=True, data={"ok": True})

    tools.register(read_thing, name="read_thing", mode="read", concurrency_safe=True)
    sidecar = Sidecar(tools=tools, llm_provider_factory=lambda _params: provider)
    sidecar._transport = _CapturingTransport()

    await _run_stream(sidecar, _base_params(execSandbox={"enabled": True}))

    assert received == ["ls"]  # not a shell tool → no rewrite
    assert "_sandbox" not in (await _tool_payloads(sidecar))[0]["data"]


@pytest.mark.asyncio
async def test_require_full_denies_when_no_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("steerable_sidecar.sidecar.seatbelt_available", lambda: False)
    received: list[str] = []
    provider = _ScriptedProvider(
        [_tool_round(ToolCall(id="c1", name="bash", arguments={"command": "ls"})),
         _text_round("denied, stopping")]
    )
    sidecar = _sidecar_with_bash(received, provider)

    await _run_stream(
        sidecar, _base_params(execSandbox={"enabled": True, "requireFull": True})
    )

    assert received == []  # denied before execution
    payload = (await _tool_payloads(sidecar))[0]
    assert payload["success"] is False
    assert payload["error"] == "sandbox_unavailable"
    assert payload["data"]["_sandbox"]["enforcement"] == "none"


@pytest.mark.skipif(not seatbelt_available(), reason="macOS sandbox-exec only")
@pytest.mark.asyncio
async def test_rewritten_command_actually_runs_confined(tmp_path) -> None:
    """End-to-end: the model's command runs under real sandbox-exec — it can
    write into a declared root and is kernel-denied outside it."""
    provider = _ScriptedProvider(
        [
            _tool_round(
                ToolCall(
                    id="c1",
                    name="bash",
                    arguments={
                        "command": f"echo hi > {tmp_path}/ok.txt && echo no > $HOME/nope-steerable"
                    },
                )
            ),
            _text_round("done"),
        ]
    )
    received: list[str] = []
    tools = ToolRouter()

    async def bash(command: str = "") -> ToolResult:
        received.append(command)
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            executable="/bin/sh",
        )
        stdout, stderr = await proc.communicate()
        return ToolResult(
            success=proc.returncode == 0,
            data={"stdout": stdout.decode(), "stderr": stderr.decode()},
        )

    tools.register(bash, name="bash", mode="other")
    sidecar = Sidecar(tools=tools, llm_provider_factory=lambda _params: provider)
    sidecar._transport = _CapturingTransport()

    await _run_stream(
        sidecar,
        _base_params(
            execSandbox={"enabled": True, "writableRoots": [str(tmp_path)]}
        ),
    )

    assert (tmp_path / "ok.txt").read_text().strip() == "hi"
    payload = (await _tool_payloads(sidecar))[0]
    assert payload["success"] is False  # $HOME write denied by the kernel
    assert "sandbox-exec" in received[0]
    assert sys.platform == "darwin"
