"""run_code: confined child, JSON tool IPC, default-off registration."""

from __future__ import annotations

from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime import CoreLoop, LoopConfig, RouterToolExecutor, ToolRouter
from steerable_agent_runtime.llm import LLMMessage

from steerable_sidecar.run_code import (
    RunCodeBoundExecutor,
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
