"""Wave 3: SandboxedToolExecutor — per-exec confinement for shell calls.

The decorator rewrites the shell tool's ``command`` argument into a
sandboxed invocation and delegates; enforcement is reported as a value
(``data["_sandbox"]``), and ``require_full`` / ``require_backend`` deny
calls that would run weaker than the requested floor instead of passing
through.
"""

from __future__ import annotations

from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall, ToolResult

from steerable_agent_runtime import (
    LoopContext,
    SandboxedToolExecutor,
)


class _RecordingExecutor:
    """Inner executor stub: records the calls it receives."""

    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
        self.calls.append(call)
        return ToolResult(success=True, data={"stdout": "ok"})


class _FakeBackend:
    name = "fake-sandbox"

    def __init__(self, enforcement: str = "full") -> None:
        self._enforcement = enforcement
        self.wrapped: list[str] = []

    @property
    def enforcement(self) -> str:
        return self._enforcement

    def wrap_command(self, command: str) -> str:
        self.wrapped.append(command)
        return f"sandbox-exec -p profile sh -c {command!r}"


def _call(name: str, **arguments: Any) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=arguments)


@pytest.mark.asyncio
async def test_shell_call_is_rewritten_and_marked() -> None:
    inner = _RecordingExecutor()
    backend = _FakeBackend()
    executor = SandboxedToolExecutor(inner, backend)

    result = await executor.execute(
        _call("bash", command="rm -rf /tmp/x"), LoopContext()
    )

    assert result.success
    # The inner executor received the sandboxed invocation, not the raw one.
    assert inner.calls[0].arguments["command"].startswith("sandbox-exec -p profile")
    assert "rm -rf /tmp/x" in inner.calls[0].arguments["command"]
    assert backend.wrapped == ["rm -rf /tmp/x"]
    # Enforcement is a return value, not a log line.
    assert result.data["_sandbox"] == {"enforcement": "full", "backend": "fake-sandbox"}


@pytest.mark.asyncio
async def test_non_shell_calls_pass_through_unmarked() -> None:
    inner = _RecordingExecutor()
    executor = SandboxedToolExecutor(inner, _FakeBackend())

    result = await executor.execute(_call("read_file", path="/a"), LoopContext())

    assert inner.calls[0].name == "read_file"
    assert "_sandbox" not in (result.data or {})


@pytest.mark.asyncio
async def test_no_backend_marks_none_and_runs() -> None:
    inner = _RecordingExecutor()
    executor = SandboxedToolExecutor(inner, None)

    result = await executor.execute(_call("bash", command="ls"), LoopContext())

    assert result.success
    assert inner.calls[0].arguments["command"] == "ls"  # unmodified
    assert result.data["_sandbox"]["enforcement"] == "none"


@pytest.mark.asyncio
async def test_require_backend_denies_when_no_backend() -> None:
    inner = _RecordingExecutor()
    executor = SandboxedToolExecutor(inner, None, require_backend=True)

    result = await executor.execute(_call("bash", command="ls"), LoopContext())

    assert not result.success
    assert result.error == "sandbox_unavailable"
    assert result.data["_sandbox"]["enforcement"] == "none"
    assert inner.calls == []


@pytest.mark.asyncio
async def test_require_backend_allows_partial() -> None:
    """The desktop's network:true makes every current backend partial.
    require_backend must not treat that as 'none'."""
    inner = _RecordingExecutor()
    executor = SandboxedToolExecutor(
        inner, _FakeBackend(enforcement="partial"), require_backend=True
    )

    result = await executor.execute(_call("bash", command="ls"), LoopContext())

    assert result.success
    assert result.data["_sandbox"]["enforcement"] == "partial"
    assert inner.calls


@pytest.mark.asyncio
async def test_require_full_denies_when_no_backend() -> None:
    inner = _RecordingExecutor()
    executor = SandboxedToolExecutor(inner, None, require_full=True)

    result = await executor.execute(_call("bash", command="ls"), LoopContext())

    assert not result.success
    assert result.error == "sandbox_unavailable"
    assert result.data["_sandbox"]["enforcement"] == "none"
    assert inner.calls == []  # denied before execution


@pytest.mark.asyncio
async def test_require_full_denies_partial_backend() -> None:
    inner = _RecordingExecutor()
    executor = SandboxedToolExecutor(
        inner, _FakeBackend(enforcement="partial"), require_full=True
    )

    result = await executor.execute(_call("bash", command="ls"), LoopContext())

    assert result.error == "sandbox_unavailable"
    assert result.data["_sandbox"]["enforcement"] == "partial"
    assert inner.calls == []


@pytest.mark.asyncio
async def test_partial_backend_runs_when_not_requiring_full() -> None:
    inner = _RecordingExecutor()
    executor = SandboxedToolExecutor(inner, _FakeBackend(enforcement="partial"))

    result = await executor.execute(_call("bash", command="ls"), LoopContext())

    assert result.success
    assert result.data["_sandbox"]["enforcement"] == "partial"


@pytest.mark.asyncio
async def test_custom_shell_tools_and_command_arg() -> None:
    inner = _RecordingExecutor()
    backend = _FakeBackend()
    executor = SandboxedToolExecutor(
        inner, backend, shell_tools={"run_script"}, command_arg="script"
    )

    await executor.execute(_call("run_script", script="echo hi"), LoopContext())
    assert backend.wrapped == ["echo hi"]
    # The default names are no longer special when overridden.
    await executor.execute(_call("bash", command="echo ho"), LoopContext())
    assert backend.wrapped == ["echo hi"]


@pytest.mark.asyncio
async def test_non_command_shaped_call_passes_through() -> None:
    inner = _RecordingExecutor()
    backend = _FakeBackend()
    executor = SandboxedToolExecutor(inner, backend)

    # Missing/empty command: inner validation owns the error, no wrapping.
    await executor.execute(_call("bash", command="  "), LoopContext())
    await executor.execute(_call("bash"), LoopContext())
    assert backend.wrapped == []
    assert len(inner.calls) == 2
