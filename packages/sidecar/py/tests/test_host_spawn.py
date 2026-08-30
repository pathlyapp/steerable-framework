"""W2.2.1: HostSpawnExecutor — confined spawn over the reverse channel.

On platforms without a command-rewriting backend (Windows), shell calls go
to the host as ``host.process.spawn`` with an explicit confinement policy.
The contract under test: policy payload shape, host-reported enforcement
surfacing as ``data["_sandbox"]``, and fail-closed behavior when the host
lacks the capability.
"""

from __future__ import annotations

from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall, ToolResult

from steerable_agent_runtime import LoopContext
from steerable_sidecar.host_spawn import HOST_SPAWN_METHOD, HostSpawnExecutor


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
        self.calls.append(call)
        return ToolResult(success=True, data={"stdout": "inner"})

    def concurrency_safe(self, call: ToolCall) -> bool:
        return True


class _FakeServer:
    """JsonRpcServer stub: records reverse calls, answers with a script."""

    def __init__(self, reply: Any = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._reply = reply
        self._error = error

    async def call(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        self.calls.append((method, params))
        if self._error is not None:
            raise self._error
        return self._reply


def _ctx() -> LoopContext:
    return LoopContext(chat_id="chat-1")


def _shell_call(command: str = "echo hi") -> ToolCall:
    return ToolCall(id="c1", name="local_exec_shell", arguments={"command": command})


POLICY = {"writableRoots": ["C:\\work"], "network": True, "allowedHosts": ["api.deepseek.com"]}


@pytest.mark.asyncio
async def test_shell_call_goes_to_host_spawn_with_policy() -> None:
    server = _FakeServer(
        reply={
            "exitCode": 0,
            "stdout": "hi",
            "stderr": "",
            "sandbox": {"backend": "windows-restricted-token", "enforcement": "full"},
        }
    )
    inner = _RecordingExecutor()
    executor = HostSpawnExecutor(inner, server, policy=POLICY)

    result = await executor.execute(_shell_call(), _ctx())

    assert inner.calls == []  # nothing ran locally
    assert len(server.calls) == 1
    method, params = server.calls[0]
    assert method == HOST_SPAWN_METHOD
    assert params["command"] == "echo hi"
    assert params["policy"] == POLICY
    assert params["context"] == {"chatId": "chat-1"}
    assert result.success is True
    assert result.data["stdout"] == "hi"
    assert result.data["_sandbox"] == {
        "backend": "windows-restricted-token",
        "enforcement": "full",
    }


@pytest.mark.asyncio
async def test_nonzero_exit_is_unsuccessful_result() -> None:
    server = _FakeServer(
        reply={"exitCode": 3, "stdout": "", "stderr": "boom",
               "sandbox": {"backend": "windows-restricted-token", "enforcement": "full"}}
    )
    executor = HostSpawnExecutor(_RecordingExecutor(), server, policy=POLICY)
    result = await executor.execute(_shell_call(), _ctx())
    assert result.success is False
    assert result.data["exitCode"] == 3
    assert result.data["stderr"] == "boom"


@pytest.mark.asyncio
async def test_missing_sandbox_report_defaults_to_none() -> None:
    """A host that omits the report gets the honest floor — the sidecar
    never upgrades enforcement on the host's behalf."""
    server = _FakeServer(reply={"exitCode": 0, "stdout": "x", "stderr": ""})
    executor = HostSpawnExecutor(_RecordingExecutor(), server, policy=POLICY)
    result = await executor.execute(_shell_call(), _ctx())
    assert result.data["_sandbox"] == {"backend": "host-spawn", "enforcement": "none"}


@pytest.mark.asyncio
async def test_host_without_capability_fails_closed() -> None:
    """Method-absent (JSON-RPC error) → tool error; the command never runs
    unsandboxed just because the host lacks the capability."""
    server = _FakeServer(error=RuntimeError("method not found"))
    inner = _RecordingExecutor()
    executor = HostSpawnExecutor(inner, server, policy=POLICY)

    result = await executor.execute(_shell_call(), _ctx())

    assert result.success is False
    assert "NOT run" in (result.error or "")
    assert inner.calls == []


@pytest.mark.asyncio
async def test_non_shell_calls_pass_through() -> None:
    server = _FakeServer()
    inner = _RecordingExecutor()
    executor = HostSpawnExecutor(inner, server, policy=POLICY)

    call = ToolCall(id="c2", name="read_file", arguments={"path": "x"})
    result = await executor.execute(call, _ctx())

    assert server.calls == []
    assert inner.calls == [call]
    assert result.data == {"stdout": "inner"}


@pytest.mark.asyncio
async def test_shell_call_without_command_passes_through() -> None:
    server = _FakeServer()
    inner = _RecordingExecutor()
    executor = HostSpawnExecutor(inner, server, policy=POLICY)

    call = ToolCall(id="c3", name="local_exec_shell", arguments={})
    await executor.execute(call, _ctx())

    assert server.calls == []
    assert inner.calls == [call]
