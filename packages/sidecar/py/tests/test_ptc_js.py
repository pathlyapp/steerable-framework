"""run_js/wait_js: persistent Node worker, session KV, yield/wait, tool bridge.

Worker tests spawn a real Node process (``node`` on PATH) behind a
passthrough sandbox backend; they skip when Node is absent. The
registration/env/scrub/descriptor tests are Node-free.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime import CoreLoop, LoopConfig, RouterToolExecutor, ToolRouter
from steerable_agent_runtime.llm import LLMMessage

from steerable_sidecar.ptc_js import (
    _worker_environ,
    ptc_js_enabled,
    register_ptc_js,
    run_js_tool_descriptor,
    wait_js_tool_descriptor,
)
from steerable_sidecar.run_code import RunCodeBoundExecutor

requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="run_js tests need a Node.js runtime"
)


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


def _passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "steerable_sidecar.ptc_js.select_exec_backend", lambda **kw: _PassthroughBackend()
    )


def _env(**overrides: str) -> dict[str, str]:
    return {**os.environ, **overrides}


def _register(
    monkeypatch: pytest.MonkeyPatch, **env_overrides: str
) -> ToolRouter:
    _passthrough(monkeypatch)
    router = ToolRouter()
    register_ptc_js(router, environ=_env(**env_overrides))
    return router


# ---------------------------------------------------------------------------
# Registration / configuration (no Node needed)
# ---------------------------------------------------------------------------


def test_ptc_js_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STEERABLE_PTC_JS", raising=False)
    assert ptc_js_enabled() is False
    assert ptc_js_enabled({"STEERABLE_PTC_JS": "1"}) is True
    router = ToolRouter()
    assert router.get("run_js") is None
    assert router.get("wait_js") is None


def test_tool_descriptor_shapes() -> None:
    run = run_js_tool_descriptor()
    assert run["type"] == "function"
    assert run["function"]["name"] == "run_js"
    assert run["function"]["parameters"]["required"] == ["code"]
    wait = wait_js_tool_descriptor()
    assert wait["function"]["name"] == "wait_js"
    assert wait["function"]["parameters"]["required"] == ["cellId"]


def test_worker_environ_is_an_allowlist() -> None:
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "TMPDIR": "/tmp",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "STEERABLE_API_KEY": "sk-secret",
        "OPENAI_API_KEY": "oa-secret",
        "STEERABLE_PTC_JS": "1",
        "ELECTRON_RUN_AS_NODE": "1",
        "STEERABLE_PTC_JS_SESSION_TTL_MS": "60000",
        "NODE_OPTIONS": "--require /tmp/evil.js",
    }
    child = _worker_environ(parent)
    assert child["PATH"] == "/usr/bin"
    assert child["LC_ALL"] == "en_US.UTF-8"
    # The desktop's Electron-as-Node switch and the worker's own knobs pass.
    assert child["ELECTRON_RUN_AS_NODE"] == "1"
    assert child["STEERABLE_PTC_JS_SESSION_TTL_MS"] == "60000"
    for leaked in (
        "STEERABLE_API_KEY",
        "OPENAI_API_KEY",
        "STEERABLE_PTC_JS",
        # NODE_OPTIONS would inject flags/modules into the worker — dropped.
        "NODE_OPTIONS",
    ):
        assert leaked not in child


@pytest.mark.asyncio
async def test_node_missing_is_node_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("steerable_sidecar.ptc_js.shutil.which", lambda _name: None)
    router = ToolRouter()
    env = {k: v for k, v in os.environ.items() if k != "STEERABLE_PTC_NODE"}
    register_ptc_js(router, environ=env)
    result = await router.dispatch(_tc("run_js", {"code": "return 1"}))
    assert result.success is False
    assert result.error == "node_unavailable"


@pytest.mark.asyncio
async def test_explicit_node_path_must_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = ToolRouter()
    register_ptc_js(
        router, environ=_env(STEERABLE_PTC_NODE="/nonexistent/node-bin")
    )
    result = await router.dispatch(_tc("run_js", {"code": "return 1"}))
    assert result.success is False
    assert result.error == "node_unavailable"
    assert "/nonexistent/node-bin" in (result.data or {}).get("message", "")


@pytest.mark.asyncio
async def test_no_backend_is_sandbox_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "steerable_sidecar.ptc_js.select_exec_backend", lambda **kw: None
    )
    router = ToolRouter()
    register_ptc_js(router, environ=_env())
    result = await router.dispatch(_tc("run_js", {"code": "return 1"}))
    assert result.success is False
    assert result.error == "sandbox_unavailable"


# ---------------------------------------------------------------------------
# Worker behavior (real Node, passthrough backend)
# ---------------------------------------------------------------------------


@requires_node
@pytest.mark.asyncio
async def test_exec_returns_value_and_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = _register(monkeypatch)
    result = await router.dispatch(
        _tc(
            "run_js",
            {
                "code": "text('hello'); console.log('dbg'); return {n: 6 * 7};",
                "description": "basic cell",
            },
        )
    )
    assert result.success is True, result.error
    data = result.data or {}
    assert data["status"] == "completed"
    assert data["value"] == {"n": 42}
    assert data["output"] == ["hello"]
    assert any("dbg" in line for line in data["logs"])
    assert data["_sandbox"]["backend"] == "test"


@requires_node
@pytest.mark.asyncio
async def test_no_node_globals_in_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _register(monkeypatch)
    result = await router.dispatch(
        _tc(
            "run_js",
            {
                "code": (
                    "return {proc: typeof process, req: typeof require, "
                    "net: typeof fetch, buf: typeof Buffer};"
                )
            },
        )
    )
    assert result.success is True, result.error
    assert (result.data or {})["value"] == {
        "proc": "undefined",
        "req": "undefined",
        "net": "undefined",
        "buf": "undefined",
    }


@requires_node
@pytest.mark.asyncio
async def test_string_code_generation_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """eval/Function inside the context throw, and the injected globals are
    context-realm wrappers whose constructor chain stays inside (the
    ``store.constructor("return process")()`` escape hits the disabled
    context Function, not the host's)."""
    router = _register(monkeypatch)
    result = await router.dispatch(
        _tc(
            "run_js",
            {
                "code": (
                    "const probe = (fn) => { try { return String(fn()); } "
                    "catch (e) { return 'blocked'; } };"
                    "return {eval_: probe(() => eval('1')), "
                    "fn: probe(() => Function('return 1')()), "
                    "escape: probe(() => store.constructor('return process')())};"
                )
            },
        )
    )
    assert result.success is True, result.error
    assert (result.data or {})["value"] == {
        "eval_": "blocked",
        "fn": "blocked",
        "escape": "blocked",
    }


@requires_node
@pytest.mark.asyncio
async def test_store_load_shared_within_one_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session KV binds to the dispatch context's chat_id and persists
    across cells; another chat sees none of it."""
    router = _register(monkeypatch)
    chat_a = {"chat_id": "chat-a"}
    stored = await router.dispatch(
        _tc("run_js", {"code": "store('k', {n: 41}); return 'stored';"}),
        context=chat_a,
    )
    assert stored.success is True, stored.error
    loaded = await router.dispatch(
        _tc("run_js", {"code": "return load('k').n + 1;"}),
        context=chat_a,
    )
    assert loaded.success is True, loaded.error
    assert (loaded.data or {})["value"] == 42
    other = await router.dispatch(
        _tc("run_js", {"code": "return typeof load('k');"}),
        context={"chat_id": "chat-b"},
    )
    assert other.success is True, other.error
    assert (other.data or {})["value"] == "undefined"


@requires_node
@pytest.mark.asyncio
async def test_yield_then_wait_returns_the_final_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cell that outlives its yield timer comes back as status "running"
    with a cellId; wait_js on that id returns the later output and the
    final value (codex's exec → wait flow)."""
    router = _register(monkeypatch)
    started = await router.dispatch(
        _tc(
            "run_js",
            {
                "code": (
                    "text('part1'); yield_control(); "
                    "await new Promise(r => setTimeout(r, 300)); "
                    "text('part2'); return 'fin';"
                ),
                "yieldTimeMs": 5000,
            },
        )
    )
    assert started.success is True, started.error
    data = started.data or {}
    assert data["status"] == "running"
    assert data["output"] == ["part1"]
    assert "cellId" in data["message"]

    waited = await router.dispatch(
        _tc("wait_js", {"cellId": data["cellId"], "yieldTimeMs": 5000})
    )
    assert waited.success is True, waited.error
    final = waited.data or {}
    assert final["status"] == "completed"
    assert final["value"] == "fin"
    # wait returns only the output accumulated since the last response.
    assert final["output"] == ["part2"]

    # The finished cell is closed: a second wait fails loud.
    again = await router.dispatch(_tc("wait_js", {"cellId": data["cellId"]}))
    assert again.success is False
    assert "unknown cell" in (again.error or "")


@requires_node
@pytest.mark.asyncio
async def test_exec_yield_timer_returns_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = _register(monkeypatch)
    started = await router.dispatch(
        _tc(
            "run_js",
            {
                "code": "await new Promise(r => setTimeout(r, 400)); return 1;",
                "yieldTimeMs": 50,
            },
        )
    )
    assert started.success is True, started.error
    assert (started.data or {})["status"] == "running"
    waited = await router.dispatch(
        _tc("wait_js", {"cellId": (started.data or {})["cellId"]})
    )
    assert waited.success is True, waited.error
    assert (waited.data or {})["status"] == "completed"


@requires_node
@pytest.mark.asyncio
async def test_wait_terminate_stops_the_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = _register(monkeypatch)
    started = await router.dispatch(
        _tc(
            "run_js",
            {
                "code": "await new Promise(r => setTimeout(r, 60000)); return 1;",
                "yieldTimeMs": 50,
            },
        )
    )
    cell_id = (started.data or {})["cellId"]
    stopped = await router.dispatch(
        _tc("wait_js", {"cellId": cell_id, "terminate": True})
    )
    assert stopped.success is False
    assert "terminated" in (stopped.error or "")
    assert (stopped.data or {})["status"] == "terminated"


@requires_node
@pytest.mark.asyncio
async def test_js_error_fails_the_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _register(monkeypatch)
    result = await router.dispatch(
        _tc("run_js", {"code": "throw new TypeError('boom');"})
    )
    assert result.success is False
    assert "TypeError" in (result.error or "")
    assert "boom" in (result.error or "")
    assert (result.data or {})["status"] == "failed"


@requires_node
@pytest.mark.asyncio
async def test_nested_run_js_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _register(monkeypatch)
    result = await router.dispatch(
        _tc("run_js", {"code": "return await tools.run_js({code: 'return 1'});"})
    )
    assert result.success is False
    assert "nested run_js is not allowed" in (result.error or "")


@requires_node
@pytest.mark.asyncio
async def test_output_budget_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _register(monkeypatch)
    result = await router.dispatch(
        _tc(
            "run_js",
            {
                "code": "text('x'.repeat(5000)); return 1;",
                "maxOutputChars": 100,
            },
        )
    )
    assert result.success is True, result.error
    data = result.data or {}
    assert data["truncated"] is True
    assert sum(len(item) for item in data["output"]) <= 200


@requires_node
@pytest.mark.asyncio
async def test_tool_calls_cap_fails_the_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = _register(monkeypatch)

    async def stub() -> ToolResult:
        return ToolResult(success=True, data={"ok": True})

    router.register(stub, name="stub", mode="read", description="s")
    result = await router.dispatch(
        _tc(
            "run_js",
            {
                "code": (
                    "for (let i = 0; i < 40; i++) { await tools.stub({}); } "
                    "return 'never';"
                )
            },
        )
    )
    assert result.success is False
    assert "exceeded" in (result.error or "")
    # The nested calls that did run are accounted on the outer result.
    assert len((result.data or {})["calls"]) == 32


@requires_node
@pytest.mark.asyncio
async def test_tool_bridge_chains_host_tools_in_one_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CoreLoop integration: one run_js call, two nested stub tools, one
    extra model round — the run_code accounting shape, through the live
    executor RunCodeBoundExecutor binds."""
    router = _register(monkeypatch)
    seen: list[str] = []

    async def stub_a() -> ToolResult:
        seen.append("a")
        return ToolResult(success=True, data={"who": "a"})

    async def stub_b() -> ToolResult:
        seen.append("b")
        return ToolResult(success=True, data={"who": "b"})

    router.register(stub_a, name="stub_a", mode="read", description="a")
    router.register(stub_b, name="stub_b", mode="read", description="b")
    code = (
        "const a = await tools.stub_a({}); "
        "const b = await tools.call('stub_b', {}); "
        "return {a: a.who, b: b.who};"
    )
    provider = _make_provider(
        [
            {"tool_calls": [_tc("run_js", {"code": code, "description": "chain"})]},
            {"content": "done"},
        ]
    )
    loop = CoreLoop(
        provider,
        RunCodeBoundExecutor(
            RouterToolExecutor(router), router=router, local_names=("run_js", "wait_js")
        ),
        LoopConfig(max_rounds=4),
    )
    events = await collect(
        loop.run(
            [LLMMessage.text_of("user", "go")],
            tools=router.describe_model(),
            chat_id="chat-js",
        )
    )
    completions = [e for e in events if e.kind == "completion"]
    assert completions[-1].data["status"] == "completed"
    assert seen == ["a", "b"]
    assert len(provider.calls) == 2
    results = [e for e in events if e.kind == "tool_call_result"]
    assert len(results) == 1
    assert results[0].data["name"] == "run_js"
    assert results[0].data["success"] is True
    tool_msgs = [m for m in provider.calls[1] if m.role == "tool"]
    assert len(tool_msgs) == 1
    body = tool_msgs[0].content_text
    assert '"who": "a"' in body or '"who":"a"' in body


@requires_node
@pytest.mark.asyncio
async def test_wedged_worker_is_killed_and_respawns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synchronous infinite loop wedges the worker's event loop — the one
    failure a vm context cannot interrupt. The Python-side response budget
    (yield + margin) expires, the worker is killed, every cell fails loud,
    and the next exec lazily spawns a fresh worker."""
    router = _register(monkeypatch, STEERABLE_PTC_JS_RESPONSE_MARGIN_MS="1000")
    wedged = await router.dispatch(
        _tc("run_js", {"code": "while (true) {}", "yieldTimeMs": 50})
    )
    assert wedged.success is False
    assert "killed" in (wedged.error or "")
    recovered = await router.dispatch(_tc("run_js", {"code": "return 'back';"}))
    assert recovered.success is True, recovered.error
    assert (recovered.data or {})["value"] == "back"


@requires_node
@pytest.mark.asyncio
async def test_confined_sidecar_inherits_layer1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A layer-1-confined sidecar must not wrap the worker again (macOS
    denies a nested sandbox_apply); the worker inherits the outer boundary
    and the result is honestly marked backend: inherited."""

    def _boom(**kw: Any) -> Any:
        raise AssertionError("select_exec_backend must not run when confined")

    monkeypatch.setattr("steerable_sidecar.ptc_js.select_exec_backend", _boom)
    router = ToolRouter()
    register_ptc_js(
        router, environ=_env(STEERABLE_SIDECAR_CONFINED="1")
    )
    result = await router.dispatch(_tc("run_js", {"code": "return 1"}))
    assert result.success is True, result.error
    sandbox = (result.data or {})["_sandbox"]
    assert sandbox["backend"] == "inherited"
    assert sandbox["enforcement"] == "partial"
