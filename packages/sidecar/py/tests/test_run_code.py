"""run_code: confined child, JSON tool IPC, default-off registration."""

from __future__ import annotations

from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime import CoreLoop, LoopConfig, RouterToolExecutor, ToolRouter
from steerable_agent_runtime.llm import LLMMessage

from steerable_sidecar.run_code import (
    RunCodeBoundExecutor,
    _mkdtemp_inheritable,
    register_run_code,
    run_code_enabled,
)
from steerable_sidecar.run_code_driver import ToolCallError, run_program


async def collect(loop_run: Any) -> list[Any]:
    return [e async for e in loop_run]


def _make_provider(script: list[dict[str, Any]]):
    from collections.abc import AsyncIterator

    from steerable_agent_runtime.llm import LLMStreamChunk

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
                content = entry.get("content", "")
                if content:
                    yield LLMStreamChunk(content_delta=content)
                for tool_call in entry.get("tool_calls", []):
                    yield LLMStreamChunk(tool_call_delta=tool_call)
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop",
                )

            return _gen()

    return _FakeProvider()


def _tc(name: str, args: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(id=f"call_{name}", name=name, arguments=args or {})


class _PassthroughBackend:
    name = "test"
    enforcement = "full"

    def argv_for_exec(self, argv: list[str]) -> list[str]:
        return list(argv)


def test_run_code_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STEERABLE_RUN_CODE", raising=False)
    assert run_code_enabled() is False
    router = ToolRouter()
    assert router.get("run_code") is None


def test_mkdtemp_inheritable_win32_avoids_0700(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """win32: the run_code tmpdir must inherit the writable-root ACEs.

    tempfile.mkdtemp hardcodes os.mkdir(dir, 0o700); CPython 3.12+ on Windows
    turns 0o700 into an owner-only DACL that drops inherited ACEs, so a
    confined sidecar (capability SID, not the owner) can no longer write
    program.py inside its own fresh dir. The helper must mkdir with the
    default mode so the parent's grants flow down.
    """
    import os
    import sys
    import tempfile
    from pathlib import Path

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    seen_modes: list[int] = []
    real_mkdir = os.mkdir

    def spy_mkdir(path: Any, mode: int = 0o777, *args: Any, **kwargs: Any) -> None:
        seen_modes.append(mode)
        real_mkdir(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", spy_mkdir)

    created = _mkdtemp_inheritable(prefix="steerable-run-code-")
    assert created.startswith(str(tmp_path))
    assert seen_modes and all(mode == 0o777 for mode in seen_modes)
    (Path(created) / "program.py").write_text("x", encoding="utf-8")


def test_mkdtemp_inheritable_posix_delegates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """POSIX keeps stock mkdtemp (0o700 semantics intact)."""
    import sys
    import tempfile

    monkeypatch.setattr(sys, "platform", "linux")
    seen_prefixes: list[str | None] = []

    def fake_mkdtemp(prefix: str | None = None, **kwargs: Any) -> str:
        seen_prefixes.append(prefix)
        return str(tmp_path / "posix-tmp")

    monkeypatch.setattr(tempfile, "mkdtemp", fake_mkdtemp)
    assert _mkdtemp_inheritable(prefix="p-") == str(tmp_path / "posix-tmp")
    assert seen_prefixes == ["p-"]


def test_driver_refuses_import_os() -> None:
    with pytest.raises(ImportError, match="os"):
        run_program("import os\nreturn os.getcwd()")


def test_driver_refuses_subprocess() -> None:
    with pytest.raises(ImportError, match="subprocess"):
        run_program("import subprocess\nreturn 1")


def test_driver_nested_run_code_raises() -> None:
    with pytest.raises(ToolCallError, match="nested"):
        run_program('return tools.call("run_code", code="return 1")')


@pytest.mark.asyncio
async def test_no_backend_is_sandbox_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "steerable_sidecar.run_code.select_exec_backend", lambda **kw: None
    )
    router = ToolRouter()
    register_run_code(router)
    result = await router.dispatch(
        ToolCall(
            id="c1",
            name="run_code",
            arguments={"code": "return 1", "description": "n"},
        )
    )
    assert result.success is False
    assert result.error == "sandbox_unavailable"


@pytest.mark.asyncio
async def test_two_stub_tools_one_coreloop_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "steerable_sidecar.run_code.select_exec_backend",
        lambda **kw: _PassthroughBackend(),
    )
    router = ToolRouter()
    seen: list[str] = []

    async def stub_a() -> ToolResult:
        seen.append("a")
        return ToolResult(success=True, data={"who": "a"})

    async def stub_b() -> ToolResult:
        seen.append("b")
        return ToolResult(success=True, data={"who": "b"})

    router.register(stub_a, name="stub_a", mode="read", description="a")
    router.register(stub_b, name="stub_b", mode="read", description="b")
    register_run_code(router)
    code = (
        "a = tools.call('stub_a')\n"
        "b = tools.call('stub_b')\n"
        "return {'a': a, 'b': b}\n"
    )
    provider = _make_provider(
        [
            {
                "tool_calls": [
                    _tc("run_code", {"code": code, "description": "two stubs"})
                ]
            },
            {"content": "done"},
        ]
    )
    loop = CoreLoop(
        provider,
        RunCodeBoundExecutor(RouterToolExecutor(router)),
        LoopConfig(max_rounds=4),
    )
    events = await collect(
        loop.run(
            [LLMMessage.text_of("user", "go")],
            tools=router.describe_model(),
        )
    )
    completions = [e for e in events if e.kind == "completion"]
    assert completions[-1].data["status"] == "completed"
    assert seen == ["a", "b"]
    assert len(provider.calls) == 2
    results = [e for e in events if e.kind == "tool_call_result"]
    assert len(results) == 1
    assert results[0].data["name"] == "run_code"
    assert results[0].data["success"] is True
    tool_msgs = [m for m in provider.calls[1] if m.role == "tool"]
    assert len(tool_msgs) == 1
    body = tool_msgs[0].content_text
    assert "stub_a" in body and "stub_b" in body


def test_child_environ_is_an_allowlist() -> None:
    from steerable_sidecar.run_code import _child_environ

    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "TMPDIR": "/tmp",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "PYTHONPATH": "/x",
        "STEERABLE_API_KEY": "sk-secret",
        "STEERABLE_RUN_CODE": "1",
        "TAVILY_API_KEY": "tv-secret",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "OPENAI_API_KEY": "oa-secret",
    }
    child = _child_environ(parent)
    assert child["PATH"] == "/usr/bin"
    assert child["HOME"] == "/home/u"
    assert child["LC_ALL"] == "en_US.UTF-8"
    assert child["PYTHONDONTWRITEBYTECODE"] == "1"
    for leaked in (
        "STEERABLE_API_KEY",
        "STEERABLE_RUN_CODE",
        "TAVILY_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "OPENAI_API_KEY",
    ):
        assert leaked not in child


def test_child_environ_win32_matches_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows stores env names in original case (``SystemRoot``, ``Path``).

    An exact-match allowlist drops them; the child then dies at Winsock init
    (WinError 10106) or DLL resolution. The scrub must match
    case-insensitively while still excluding credentials.
    """
    import sys

    from steerable_sidecar.run_code import _child_environ

    monkeypatch.setattr(sys, "platform", "win32")
    parent = {
        "Path": "C:\\Windows\\System32",
        "SystemRoot": "C:\\Windows",
        "TEMP": "C:\\Users\\u\\AppData\\Local\\Temp",
        "STEERABLE_API_KEY": "sk-secret",
        "OPENAI_API_KEY": "oa-secret",
    }
    child = _child_environ(parent)
    assert child["Path"] == "C:\\Windows\\System32"
    assert child["SystemRoot"] == "C:\\Windows"
    assert child["TEMP"] == "C:\\Users\\u\\AppData\\Local\\Temp"
    assert "STEERABLE_API_KEY" not in child
    assert "OPENAI_API_KEY" not in child


def test_run_code_tool_descriptor_shape() -> None:
    from steerable_sidecar.run_code import run_code_tool_descriptor

    d = run_code_tool_descriptor()
    assert d["type"] == "function"
    assert d["function"]["name"] == "run_code"
    assert d["function"]["parameters"]["required"] == ["code", "description"]


@pytest.mark.asyncio
async def test_confined_sidecar_inherits_layer1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A layer-1-confined sidecar must not attempt a nested wrap.

    macOS denies a second ``sandbox_apply`` under an outer profile that allows
    outbound network, so the child runs unwrapped and the result is marked
    ``backend: inherited`` / ``enforcement: partial``.
    """
    def _boom(**kw: Any) -> Any:
        raise AssertionError("select_exec_backend must not run when confined")

    monkeypatch.setattr("steerable_sidecar.run_code.select_exec_backend", _boom)
    router = ToolRouter()

    async def stub() -> ToolResult:
        return ToolResult(success=True, data={"who": "stub"})

    router.register(stub, name="stub", mode="read", description="s")
    # The stub environ must overlay the real one: a bare-dict environ strips
    # SystemRoot/Path on Windows, and the child interpreter then dies at
    # Winsock init (WinError 10106) before the program even runs.
    import os

    register_run_code(
        router, environ={**os.environ, "STEERABLE_SIDECAR_CONFINED": "1"}
    )
    result = await router.dispatch(
        ToolCall(
            id="c1",
            name="run_code",
            arguments={
                "code": "return tools.call('stub')",
                "description": "inherit layer-1",
            },
        )
    )
    assert result.success is True, result.error
    sandbox = (result.data or {})["_sandbox"]
    assert sandbox["backend"] == "inherited"
    assert sandbox["enforcement"] == "partial"
