"""agent.chat.stream via CoreLoop (useCoreLoop flag / STEERABLE_SIDECAR_CORELOOP)."""

from __future__ import annotations

import asyncio

import pytest
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime.llm import LLMStreamChunk, LLMUsage

from steerable_sidecar.sidecar import Sidecar


class _ScriptedProvider:
    """Plays a fixed script of rounds; can fail on chosen stream attempts."""

    name = "scripted"
    model = "scripted-model"

    def __init__(self, script: list[list[LLMStreamChunk]], fail_on: set[int] | None = None):
        self._script = script
        self._fail_on = fail_on or set()
        self.attempts = 0
        self._round = 0
        self.stream_kwargs: list[dict] = []

    async def complete(self, *args, **kwargs):
        raise NotImplementedError

    def stream(self, messages, **kwargs):
        self.attempts += 1
        attempt = self.attempts
        self.stream_kwargs.append(dict(kwargs))
        chunks = self._script[min(self._round, len(self._script) - 1)]

        async def _gen():
            if attempt in self._fail_on:
                raise RuntimeError("upstream blew up")
                yield  # pragma: no cover — make this a generator
            self._round += 1
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


def _make_sidecar(provider: _ScriptedProvider, **kwargs) -> Sidecar:
    sidecar = Sidecar(llm_provider_factory=lambda _params: provider, **kwargs)
    sidecar._transport = _CapturingTransport()  # type: ignore[attr-defined]
    return sidecar


async def _run_stream(sidecar: Sidecar, params: dict) -> tuple[str, list[tuple[str, dict]]]:
    response = await sidecar.server.handle_frame(
        _frame("agent.chat.stream", params)
    )
    assert "error" not in response, response
    stream_id = response["result"]["streamId"]
    task = sidecar._streams.get(stream_id)
    if task is not None:
        await task
    return stream_id, sidecar._transport.events  # type: ignore[attr-defined]


def _frame(method: str, params: dict | None = None) -> str:
    import json

    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


@pytest.mark.asyncio
async def test_coreloop_path_streams_content_and_done() -> None:
    provider = _ScriptedProvider([_text_round("hello world")])
    sidecar = _make_sidecar(provider)

    stream_id, events = await _run_stream(
        sidecar,
        {
            "provider": "openai_compat",
            "model": "fake",
            "messages": [{"role": "user", "content": "hi"}],
            "useCoreLoop": True,
        },
    )

    chunks = [p for m, p in events if m == "stream.chunk"]
    assert any(c.get("delta") == "hello world" for c in chunks)
    done = [p for m, p in events if m == "stream.done"]
    assert len(done) == 1
    assert done[0]["streamId"] == stream_id
    assert done[0]["ok"] is True
    assert done[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_coreloop_path_executes_tool_round() -> None:
    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="c1", name="add", arguments={"a": 1, "b": 2})),
            _text_round("sum is 3"),
        ]
    )
    sidecar = _make_sidecar(provider)

    async def add(a: int, b: int) -> int:
        return a + b

    sidecar.tools.register(add)

    _stream_id, events = await _run_stream(
        sidecar,
        {
            "provider": "openai_compat",
            "model": "fake",
            "messages": [{"role": "user", "content": "add"}],
            "useCoreLoop": True,
        },
    )

    chunks = [p for m, p in events if m == "stream.chunk"]
    tool_calls = [c["toolCall"] for c in chunks if "toolCall" in c]
    tool_results = [c["toolResult"] for c in chunks if "toolResult" in c]
    assert tool_calls == [{"id": "c1", "name": "add", "arguments": {"a": 1, "b": 2}}]
    assert len(tool_results) == 1 and tool_results[0]["success"] is True
    assert any(c.get("delta") == "sum is 3" for c in chunks)
    done = [p for m, p in events if m == "stream.done"]
    assert done[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_coreloop_path_retries_transient_errors_by_default() -> None:
    # RetryHooks is the default hooks impl on this path: one transient stream
    # failure must not kill the run.
    provider = _ScriptedProvider([_text_round("recovered")], fail_on={1})
    sidecar = _make_sidecar(provider)

    _stream_id, events = await _run_stream(
        sidecar,
        {
            "provider": "openai_compat",
            "model": "fake",
            "messages": [{"role": "user", "content": "hi"}],
            "useCoreLoop": True,
        },
    )

    assert provider.attempts == 2
    done = [p for m, p in events if m == "stream.done"]
    assert done[0]["ok"] is True


@pytest.mark.asyncio
async def test_legacy_path_is_default_when_flag_absent() -> None:
    provider = _ScriptedProvider([_text_round("hi")])
    sidecar = _make_sidecar(provider)

    _stream_id, events = await _run_stream(
        sidecar,
        {
            "provider": "openai_compat",
            "model": "fake",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    # legacy path: raw chunks (incl. finishReason/usage on the last one) and
    # a bare stream.done without loop status fields
    done = [p for m, p in events if m == "stream.done"]
    assert done == [{"streamId": _stream_id, "ok": True}]
    last_chunk = [p for m, p in events if m == "stream.chunk"][-1]
    assert last_chunk["usage"]["totalTokens"] == 6


@pytest.mark.asyncio
async def test_env_var_enables_coreloop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEERABLE_SIDECAR_CORELOOP", "1")
    provider = _ScriptedProvider([_text_round("env path")])
    sidecar = _make_sidecar(provider)

    _stream_id, events = await _run_stream(
        sidecar,
        {
            "provider": "openai_compat",
            "model": "fake",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    done = [p for m, p in events if m == "stream.done"]
    assert done[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_coreloop_path_reports_terminal_failure() -> None:
    # Persistent stream failure: RetryHooks exhausts, loop emits error + a
    # failed completion, which must surface as stream.error + stream.done.
    provider = _ScriptedProvider([_text_round("never")], fail_on={1, 2, 3, 4, 5})
    sidecar = _make_sidecar(provider)

    _stream_id, events = await _run_stream(
        sidecar,
        {
            "provider": "openai_compat",
            "model": "fake",
            "messages": [{"role": "user", "content": "hi"}],
            "useCoreLoop": True,
        },
    )

    errors = [p for m, p in events if m == "stream.error"]
    done = [p for m, p in events if m == "stream.done"]
    assert len(errors) == 1
    assert done[0]["ok"] is False
    assert done[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_coreloop_stream_persists_trace_fetchable() -> None:
    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="c1", name="add", arguments={"a": 1, "b": 2})),
            _text_round("sum is 3"),
        ]
    )
    sidecar = _make_sidecar(provider)

    _stream_id, events = await _run_stream(
        sidecar,
        {
            "provider": "openai_compat",
            "model": "fake",
            "messages": [{"role": "user", "content": "add"}],
            "useCoreLoop": True,
        },
    )

    done = [p for m, p in events if m == "stream.done"]
    trace_id = done[0].get("traceId")
    assert trace_id, "stream.done should carry the recorder's traceId"

    response = await sidecar.server.handle_frame(
        _frame("trace.fetch", {"traceId": trace_id})
    )
    assert "error" not in response, response
    result = response["result"]
    assert result["trace"]["status"] == "completed"
    assert result["trace"]["spanCount"] == 1  # one tool span
    assert result["spans"][0]["name"] == "add"
    kinds = [e["kind"] for e in result["events"]]
    assert "tool_call_start" in kinds and "tool_call_result" in kinds


@pytest.mark.asyncio
async def test_coreloop_antihallucination_deferred_retry() -> None:
    """antiHallucination: true wires the desktop's deferred-execution guard
    into the sidecar CoreLoop: an all-talk-no-tool_call round is sent back
    with a discipline notice and the turn retried."""
    provider = _ScriptedProvider(
        [
            _text_round("任务已排队，任务 ID 为 t_1。现在轮询结果。"),
            _tool_round(ToolCall(id="c1", name="add", arguments={"a": 1, "b": 2})),
            _text_round("sum is 3"),
        ]
    )
    sidecar = _make_sidecar(provider)

    async def add(a: int, b: int) -> int:
        return a + b

    sidecar.tools.register(add)

    _stream_id, events = await _run_stream(
        sidecar,
        {
            "provider": "openai_compat",
            "model": "fake",
            "messages": [{"role": "user", "content": "执行 add 计算"}],
            "useCoreLoop": True,
            "antiHallucination": True,
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "add", "description": "add", "parameters": {"type": "object"}},
                }
            ],
        },
    )

    # 初始轮（空话）+ 纪律重试轮（工具）+ 收尾轮 = 3 次 LLM 调用
    assert provider.attempts == 3
    # 路由分类失败（complete 未实现）回落 require_tool → 首轮强制 tool_choice
    assert provider.stream_kwargs[0].get("tool_choice") == "required"
    tool_results = [c["toolResult"] for c in (p for m, p in events if m == "stream.chunk") if "toolResult" in c]
    assert len(tool_results) == 1 and tool_results[0]["success"] is True
    done = [p for m, p in events if m == "stream.done"]
    assert done[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_coreloop_antihallucination_off_by_default() -> None:
    """Without the flag the same all-talk round ends the turn (no retry)."""
    provider = _ScriptedProvider(
        [_text_round("任务已排队，任务 ID 为 t_1。现在轮询结果。")]
    )
    sidecar = _make_sidecar(provider)

    _stream_id, events = await _run_stream(
        sidecar,
        {
            "provider": "openai_compat",
            "model": "fake",
            "messages": [{"role": "user", "content": "执行 add 计算"}],
            "useCoreLoop": True,
        },
    )

    assert provider.attempts == 1
    assert provider.stream_kwargs[0].get("tool_choice") is None
    done = [p for m, p in events if m == "stream.done"]
    assert done[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_coreloop_steer_injects_into_running_turn() -> None:
    """agent.chat.steer lands the user message in the running loop's
    transcript: the next LLM round sees it, and a steer notice comes back
    on the wire."""
    router_tool_started = asyncio.Event()
    proceed = asyncio.Event()

    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="c1", name="add", arguments={"a": 1, "b": 2})),
            _text_round("done"),
        ]
    )
    sidecar = _make_sidecar(provider)

    async def add(a: int, b: int) -> int:
        router_tool_started.set()
        await proceed.wait()  # hold the turn open so steer can land mid-run
        return a + b

    sidecar.tools.register(add)

    response = await sidecar.server.handle_frame(
        _frame(
            "agent.chat.stream",
            {
                "provider": "openai_compat",
                "model": "fake",
                "messages": [{"role": "user", "content": "add"}],
                "useCoreLoop": True,
            },
        )
    )
    assert "error" not in response, response
    stream_id = response["result"]["streamId"]

    # wait until the tool is actually executing, then steer
    await asyncio.wait_for(router_tool_started.wait(), timeout=2)
    steer_resp = await sidecar.server.handle_frame(
        _frame("agent.chat.steer", {"streamId": stream_id, "content": "顺便乘以 2"})
    )
    assert steer_resp["result"] == {"ok": True}
    proceed.set()

    task = sidecar._streams.get(stream_id)
    if task is not None:
        await task

    events = sidecar._transport.events  # type: ignore[attr-defined]
    notices = [
        p["notice"]
        for m, p in events
        if m == "stream.chunk" and "notice" in p
    ]
    assert any(n.get("kind") == "steer" and n.get("content") == "顺便乘以 2" for n in notices)
    done = [p for m, p in events if m == "stream.done"]
    assert done[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_steer_unknown_stream_soft_fails() -> None:
    sidecar = _make_sidecar(_ScriptedProvider([_text_round("x")]))
    resp = await sidecar.server.handle_frame(
        _frame("agent.chat.steer", {"streamId": "nope", "content": "hi"})
    )
    assert resp["result"] == {"ok": False, "reason": "stream_not_active"}


@pytest.mark.asyncio
async def test_steer_requires_content() -> None:
    sidecar = _make_sidecar(_ScriptedProvider([_text_round("x")]))
    resp = await sidecar.server.handle_frame(
        _frame("agent.chat.steer", {"streamId": "s1", "content": "  "})
    )
    assert "error" in resp


def test_default_loop_hooks_resolves_window_from_model() -> None:
    """Fixed-60k default is gone: known models compact against their real
    context window; explicit maxContextTokens still wins."""
    from steerable_agent_runtime import ChainHooks, CompactionHooks
    from steerable_sidecar.sidecar import _default_loop_hooks

    hooks = _default_loop_hooks({"model": "gpt-oss:20b-cloud"})
    assert isinstance(hooks, ChainHooks)
    compaction = next(h for h in hooks._hooks if isinstance(h, CompactionHooks))
    assert compaction._max_tokens == 131_072

    explicit = _default_loop_hooks(
        {"model": "gpt-oss:20b-cloud", "maxContextTokens": 24_000}
    )
    compaction = next(
        h for h in explicit._hooks if isinstance(h, CompactionHooks)
    )
    assert compaction._max_tokens == 24_000

    unknown = _default_loop_hooks({"model": "my-finetune"})
    compaction = next(
        h for h in unknown._hooks if isinstance(h, CompactionHooks)
    )
    assert compaction._max_tokens == 60_000
