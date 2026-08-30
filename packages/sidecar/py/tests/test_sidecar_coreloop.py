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
        self.seen_messages: list[list] = []

    async def complete(self, *args, **kwargs):
        raise NotImplementedError

    def stream(self, messages, **kwargs):
        self.attempts += 1
        attempt = self.attempts
        self.stream_kwargs.append(dict(kwargs))
        self.seen_messages.append(list(messages))
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
async def test_trace_export_pushes_redacted_payload_to_collector() -> None:
    """trace.export (W6-6): the sidecar pushes a stored trace to an OTLP/HTTP
    collector, secret-redacted, defaulting to metadata privacy mode."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received: dict = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="c1", name="add", arguments={"a": 1, "b": 2})),
            _text_round("sum is 3"),
        ]
    )
    sidecar = _make_sidecar(provider)
    try:
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
        assert trace_id

        response = await sidecar.server.handle_frame(
            _frame(
                "trace.export",
                {
                    "traceId": trace_id,
                    "endpoint": f"http://127.0.0.1:{server.server_port}/v1/traces",
                },
            )
        )
        assert "error" not in response, response
        assert response["result"]["status"] == 200
        assert response["result"]["privacyMode"] == "metadata"
        spans = received["body"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert spans[0]["name"] == "coreloop.run"

        # invalid privacy mode is rejected
        bad = await sidecar.server.handle_frame(
            _frame(
                "trace.export",
                {
                    "traceId": trace_id,
                    "endpoint": f"http://127.0.0.1:{server.server_port}/v1/traces",
                    "privacyMode": "everything",
                },
            )
        )
        assert "error" in bad
    finally:
        server.shutdown()




@pytest.mark.asyncio
async def test_stream_done_carries_accumulated_usage_and_cost() -> None:
    """W6-9: stream.done carries the run's accumulated billable usage, plus a
    cost estimate when the model is priced (omitted for unpriced models)."""
    # Priced model → costUsd present. Two rounds × (5 in / 1 out) = 10/2/12.
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
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "add"}],
            "useCoreLoop": True,
        },
    )
    done = [p for m, p in events if m == "stream.done"]
    usage = done[0]["usage"]
    assert usage["promptTokens"] == 10
    assert usage["completionTokens"] == 2
    assert usage["totalTokens"] == 12
    # deepseek-chat: $0.27/1M in, $1.10/1M out → (10*0.27 + 2*1.10)/1e6
    assert usage["costUsd"] == pytest.approx((10 * 0.27 + 2 * 1.10) / 1_000_000)

    # Unpriced (local/unknown) model → usage present, costUsd omitted.
    provider2 = _ScriptedProvider([_text_round("hi")])
    sidecar2 = _make_sidecar(provider2)
    _s2, events2 = await _run_stream(
        sidecar2,
        {
            "provider": "openai_compat",
            "model": "fake",
            "messages": [{"role": "user", "content": "hi"}],
            "useCoreLoop": True,
        },
    )
    done2 = [p for m, p in events2 if m == "stream.done"]
    assert done2[0]["usage"]["totalTokens"] == 6
    assert "costUsd" not in done2[0]["usage"]


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
async def test_coreloop_cancel_winds_down_cooperatively() -> None:
    """agent.chat.cancel on a CoreLoop stream is cooperative: the loop
    asyncio-cancels the in-flight tool, records the partial turn, and the
    terminal stream.done carries status="cancelled" — not a hard task kill."""
    tool_started = asyncio.Event()
    tool_cancelled = False

    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="c1", name="hanging", arguments={})),
            _text_round("never reached"),
        ]
    )
    sidecar = _make_sidecar(provider)

    async def hanging() -> str:
        nonlocal tool_cancelled
        tool_started.set()
        try:
            await asyncio.Event().wait()  # held open so cancel lands mid-tool
        except asyncio.CancelledError:
            tool_cancelled = True
            raise
        return "done"  # pragma: no cover

    sidecar.tools.register(hanging)

    response = await sidecar.server.handle_frame(
        _frame(
            "agent.chat.stream",
            {
                "provider": "openai_compat",
                "model": "fake",
                "messages": [{"role": "user", "content": "go"}],
                "useCoreLoop": True,
            },
        )
    )
    assert "error" not in response, response
    stream_id = response["result"]["streamId"]

    await asyncio.wait_for(tool_started.wait(), timeout=2)
    cancel_resp = await sidecar.server.handle_frame(
        _frame("agent.chat.cancel", {"streamId": stream_id})
    )
    assert "error" not in cancel_resp, cancel_resp

    task = sidecar._streams.get(stream_id)
    if task is not None:
        await asyncio.wait_for(task, timeout=2)

    assert tool_cancelled, "in-flight tool coroutine was not asyncio-cancelled"
    events = sidecar._transport.events  # type: ignore[attr-defined]
    done = [p for m, p in events if m == "stream.done"]
    assert len(done) == 1
    assert done[0]["ok"] is False
    assert done[0]["status"] == "cancelled"
    assert done[0]["cancelled"] is True


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




def test_default_loop_hooks_wires_summarizer(monkeypatch) -> None:
    """The framework is now the sole owner of cross-turn compaction, so the
    turn provider is wired as the summarizer: compaction makes a genuine model
    call (the desktop rolling summary's quality bar), not the deterministic
    excerpt fallback. STEERABLE_SIDECAR_SUMMARIZER=0 opts out."""
    from steerable_agent_runtime import CompactionHooks
    from steerable_sidecar.sidecar import _default_loop_hooks, _summarizer_for

    provider = _ScriptedProvider([_text_round("summary")])
    hooks = _default_loop_hooks({"model": "gpt-oss:20b-cloud"}, summarizer=provider)
    compaction = next(h for h in hooks._hooks if isinstance(h, CompactionHooks))
    assert compaction._summarizer is provider

    # Default: the turn provider is reused as the summarizer.
    assert _summarizer_for(provider) is provider
    # Opt-out keeps the deterministic excerpt fallback.
    monkeypatch.setenv("STEERABLE_SIDECAR_SUMMARIZER", "0")
    assert _summarizer_for(provider) is None










@pytest.mark.asyncio
async def test_chat_fork_seeds_from_trace_projection() -> None:
    """fork = project the source trace, append the re-asked user turn, run a
    new CoreLoop stream that records its own trace (variant semantics)."""
    provider = _ScriptedProvider([_text_round("first answer"), _text_round("second answer")])
    sidecar = _make_sidecar(provider)

    _sid, events = await _run_stream(
        sidecar,
        {
            "provider": "openai_compat",
            "model": "fake",
            "messages": [{"role": "user", "content": "q1"}],
            "useCoreLoop": True,
        },
    )
    done = next(p for m, p in events if m == "stream.done")
    trace_id = done["traceId"]

    resp = await sidecar.server.handle_frame(
        _frame(
            "agent.chat.fork",
            {
                "provider": "openai_compat",
                "model": "fake",
                "traceId": trace_id,
                "messages": [{"role": "user", "content": "q2"}],
            },
        )
    )
    assert "error" not in resp, resp
    # The seed is the projected assistant reply (the user turn was loop
    # input, not an event); the re-asked user message is appended.
    assert resp["result"]["seedMessages"] == 2
    task = sidecar._streams.get(resp["result"]["streamId"])
    if task is not None:
        await task

    fork_call = provider.seen_messages[-1]
    assert [m.role for m in fork_call] == ["assistant", "user"]
    assert fork_call[0].content_text == "first answer"
    assert fork_call[1].content_text == "q2"


@pytest.mark.asyncio
async def test_chat_fork_until_sequence_truncates() -> None:
    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="c1", name="add", arguments={"a": 1, "b": 2})),
            _text_round("sum is 3"),
            _text_round("forked"),
        ]
    )
    sidecar = _make_sidecar(provider)

    async def add(a: int, b: int) -> int:
        return a + b

    sidecar.tools.register(add)
    _sid, events = await _run_stream(
        sidecar,
        {
            "provider": "openai_compat",
            "model": "fake",
            "messages": [{"role": "user", "content": "add"}],
            "useCoreLoop": True,
        },
    )
    trace_id = next(p for m, p in events if m == "stream.done")["traceId"]
    stored = await sidecar.storage.list_events(trace_id)
    result_event = next(e for e in stored if e.kind == "tool_call_result")

    resp = await sidecar.server.handle_frame(
        _frame(
            "agent.chat.fork",
            {
                "provider": "openai_compat",
                "model": "fake",
                "traceId": trace_id,
                "untilSequence": result_event.sequence,
                "messages": [{"role": "user", "content": "try again"}],
            },
        )
    )
    assert "error" not in resp, resp
    # assistant(tool_call) + tool(result) survive; round 1's text is cut.
    assert resp["result"]["seedMessages"] == 3
    task = sidecar._streams.get(resp["result"]["streamId"])
    if task is not None:
        await task
    fork_call = provider.seen_messages[-1]
    assert [m.role for m in fork_call] == ["assistant", "tool", "user"]


@pytest.mark.asyncio
async def test_chat_fork_unknown_trace_errors() -> None:
    sidecar = _make_sidecar(_ScriptedProvider([_text_round("x")]))
    resp = await sidecar.server.handle_frame(
        _frame(
            "agent.chat.fork",
            {"provider": "openai_compat", "model": "fake", "traceId": "nope"},
        )
    )
    assert "error" in resp


def test_build_loop_config_default_budget_scales_with_window() -> None:
    """No explicit budgetTokens → 2× the model's context window (production-
    calibrated: mean trace ≈ 1.1× window; the old fixed 120k api cap cut 6%
    of real tasks). Explicit budgetTokens still wins."""
    from steerable_sidecar.sidecar import _build_loop_config

    cfg = _build_loop_config({"model": "deepseek-v4"})
    assert cfg.budget is not None
    assert cfg.budget.max_tokens == 2 * 131_072

    cfg_unknown = _build_loop_config({})
    assert cfg_unknown.budget is not None
    assert cfg_unknown.budget.max_tokens == 2 * 60_000

    cfg_explicit = _build_loop_config({"model": "deepseek-v4", "budgetTokens": 50_000})
    assert cfg_explicit.budget is not None
    assert cfg_explicit.budget.max_tokens == 50_000


def test_build_loop_config_tool_timeout_wiring() -> None:
    """toolTimeoutMs overrides the LoopConfig default; absent keeps it."""
    from steerable_sidecar.sidecar import _build_loop_config

    assert _build_loop_config({}).tool_timeout_ms == 300_000
    assert _build_loop_config({"toolTimeoutMs": 5_000}).tool_timeout_ms == 5_000


@pytest.mark.asyncio
async def test_subagent_optin_advertises_and_executes_delegation() -> None:
    """params.subagent wraps the executor and appends the tool descriptor;
    a delegate_subagent call is answered by a bounded child CoreLoop."""
    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="d1", name="delegate_subagent", arguments={"task": "compute"})),
            _text_round("child answer"),
            _text_round("parent final"),
        ]
    )
    sidecar = _make_sidecar(provider)

    _sid, events = await _run_stream(
        sidecar,
        {
            "provider": "openai_compat",
            "model": "fake",
            "messages": [{"role": "user", "content": "delegate"}],
            "useCoreLoop": True,
            "subagent": True,
        },
    )

    # The descriptor reached the model's tool list.
    first_tools = provider.stream_kwargs[0].get("tools") or []
    assert any(t["function"]["name"] == "delegate_subagent" for t in first_tools)
    # The delegation ran a child loop whose answer became the tool result,
    # and the parent completed.
    done = [p for m, p in events if m == "stream.done"]
    assert done[0]["ok"] is True
    assert done[0]["status"] == "completed"
    chunks = [p for m, p in events if m == "stream.chunk"]
    tool_results = [c for c in chunks if c.get("toolResult")]
    assert any("child answer" in str(tr) for tr in tool_results)


@pytest.mark.asyncio
async def test_subagent_off_by_default() -> None:
    provider = _ScriptedProvider([_text_round("plain")])
    sidecar = _make_sidecar(provider)
    await _run_stream(
        sidecar,
        {
            "provider": "openai_compat",
            "model": "fake",
            "messages": [{"role": "user", "content": "hi"}],
            "useCoreLoop": True,
        },
    )
    first_tools = provider.stream_kwargs[0].get("tools") or []
    assert not any(t["function"]["name"] == "delegate_subagent" for t in first_tools)




@pytest.mark.asyncio
async def test_chat_fork_from_record_seeds_a_fresh_log() -> None:
    """Wave 1 record fork: the variant runs under a fresh record id seeded
    by one provenance-carrying history.seed entry; the source chat's log is
    never polluted by the variant."""
    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="c1", name="add", arguments={"a": 1, "b": 2})),
            _text_round("sum is 3"),
            _text_round("forked answer"),
        ]
    )
    sidecar = _make_sidecar(provider)

    async def add(a: int, b: int) -> int:
        return a + b

    sidecar.tools.register(add)
    await _run_stream(
        sidecar,
        {
            "provider": "openai_compat",
            "model": "fake",
            "messages": [{"role": "user", "content": "add"}],
            "useCoreLoop": True,
            "chatId": "chat_1",
        },
    )
    source_before = await sidecar.storage.list_history("chat_1")
    assert len(source_before) == 4  # user, assistant(c1), tool, assistant
    # Fork after the tool result (seq 2), cutting the "sum is 3" answer.
    resp = await sidecar.server.handle_frame(
        _frame(
            "agent.chat.fork",
            {
                "provider": "openai_compat",
                "model": "fake",
                "recordId": "chat_1",
                "untilSeq": 2,
                "messages": [{"role": "user", "content": "try again"}],
            },
        )
    )
    assert "error" not in resp, resp
    assert resp["result"]["seedMessages"] == 4  # 3 seeded + re-asked user
    fork_stream = resp["result"]["streamId"]
    task = sidecar._streams.get(fork_stream)
    if task is not None:
        await task

    # The model saw the truncated prefix + the re-asked user message.
    fork_call = provider.seen_messages[-1]
    assert [m.role for m in fork_call] == ["user", "assistant", "tool", "user"]
    assert fork_call[3].content_text == "try again"

    # The fork's record: one provenance seed + only the genuinely new items
    # (the seed entry already covers the prefix, so the run's seed items
    # keep their in-memory seqs 1-3 unpersisted — seq is monotonic, not
    # dense).
    fork_record = await sidecar.storage.list_history(f"chat_1:fork:{fork_stream}")
    assert fork_record[0]["entry"] == "seed"
    assert fork_record[0]["source_record_id"] == "chat_1"
    assert fork_record[0]["source_until_seq"] == 2
    assert [e["seq"] for e in fork_record] == [0, 4, 5]
    assert fork_record[1]["message"]["content"] == [
        {"type": "text", "text": "try again"}
    ]
    assert fork_record[2]["message"]["content"] == [
        {"type": "text", "text": "forked answer"}
    ]
    # And the fork's record resumes self-contained (never reads chat_1).
    from steerable_agent_runtime import load_history_transcript

    resumed = await load_history_transcript(
        sidecar.storage, f"chat_1:fork:{fork_stream}"
    )
    assert [m.content_text for m in resumed] == [
        "add",
        fork_call[1].content_text,
        fork_call[2].content_text,
        "try again",
        "forked answer",
    ]

    # The source chat's log is untouched by the variant.
    assert await sidecar.storage.list_history("chat_1") == source_before


@pytest.mark.asyncio
async def test_session_fork_creates_branch_without_running_a_turn() -> None:
    """agent.session.fork (Wave 5): the non-destructive regen primitive —
    forks the record, returns the BranchPoint, runs nothing."""
    provider = _ScriptedProvider([_text_round("answer 0"), _text_round("answer 1")])
    sidecar = _make_sidecar(provider)
    # Host-shaped turns: each passes the FULL intended transcript prefix (as
    # the desktop does), so the record accumulates one continuous span.
    history: list[dict] = []
    for question, answer in (("question 0", "answer 0"), ("question 1", "answer 1")):
        history.append({"role": "user", "content": question})
        await _run_stream(
            sidecar,
            {
                "provider": "openai_compat",
                "model": "fake",
                "messages": list(history),
                "useCoreLoop": True,
                "chatId": "chat_1",
            },
        )
        history.append({"role": "assistant", "content": answer})
    source_before = await sidecar.storage.list_history("chat_1")
    attempts_before = provider.attempts

    resp = await sidecar.server.handle_frame(
        _frame(
            "agent.session.fork",
            {"recordId": "chat_1", "beforeLastUser": True, "newRecordId": "chat_1:r2"},
        )
    )

    assert "error" not in resp, resp
    result = resp["result"]
    assert result["recordId"] == "chat_1:r2"
    assert result["sourceRecordId"] == "chat_1"
    # beforeLastUser: the fork keeps the prompting user turn, drops the
    # assistant reply after it.
    assert result["label"] == "question 1"
    assert result["seedMessages"] == 3  # q0, a0, q1
    # No turn ran on the branch.
    assert provider.attempts == attempts_before
    # Source record untouched; the branch is one provenance seed.
    assert await sidecar.storage.list_history("chat_1") == source_before
    branch = await sidecar.storage.list_history("chat_1:r2")
    assert len(branch) == 1
    assert branch[0]["entry"] == "seed"
    assert branch[0]["source_record_id"] == "chat_1"


@pytest.mark.asyncio
async def test_session_fork_unknown_record_errors() -> None:
    sidecar = _make_sidecar(_ScriptedProvider([_text_round("x")]))
    resp = await sidecar.server.handle_frame(
        _frame("agent.session.fork", {"recordId": "nope"})
    )
    assert "error" in resp


@pytest.mark.asyncio
async def test_session_branches_reports_lineage_and_children() -> None:
    provider = _ScriptedProvider([_text_round("answer 0")])
    sidecar = _make_sidecar(provider)
    await _run_stream(
        sidecar,
        {
            "provider": "openai_compat",
            "model": "fake",
            "messages": [{"role": "user", "content": "question 0"}],
            "useCoreLoop": True,
            "chatId": "chat_1",
        },
    )
    for branch_id in ("chat_1:r2", "chat_1:r3"):
        resp = await sidecar.server.handle_frame(
            _frame(
                "agent.session.fork",
                {"recordId": "chat_1", "newRecordId": branch_id},
            )
        )
        assert "error" not in resp, resp

    # From the root: children discovered via the record enumeration.
    resp = await sidecar.server.handle_frame(
        _frame("agent.session.branches", {"recordId": "chat_1"})
    )
    assert "error" not in resp, resp
    result = resp["result"]
    assert [p["recordId"] for p in result["lineage"]] == ["chat_1"]
    assert result["lineage"][0]["depth"] == 0
    assert sorted(c["recordId"] for c in result["children"]) == [
        "chat_1:r2",
        "chat_1:r3",
    ]
    assert all(c["label"] == "question 0" for c in result["children"])

    # From a branch: lineage walks up to the root.
    resp = await sidecar.server.handle_frame(
        _frame("agent.session.branches", {"recordId": "chat_1:r2"})
    )
    assert "error" not in resp, resp
    chain = resp["result"]["lineage"]
    assert [p["recordId"] for p in chain] == ["chat_1", "chat_1:r2"]
    assert [p["depth"] for p in chain] == [0, 1]
    assert resp["result"]["children"] == []


class _ContentRoutedProvider:
    """Routes by the transcript's first user message — parent and child
    scripts stay deterministic no matter how their requests interleave."""

    name = "routed"
    model = "routed-model"

    def __init__(self, routes: dict[str, list[list[LLMStreamChunk]]]):
        self._routes = routes
        self._counters = {key: 0 for key in routes}
        self.stream_kwargs: list[dict] = []

    async def complete(self, *args, **kwargs):
        raise NotImplementedError

    def stream(self, messages, **kwargs):
        self.stream_kwargs.append(dict(kwargs))
        first_user = next((m for m in messages if m.role == "user"), None)
        key = next(
            (k for k in self._routes if first_user is not None and k in first_user.content_text),
            None,
        )
        assert key is not None, f"no route for {first_user!r}"
        idx = self._counters[key]
        self._counters[key] += 1
        script = self._routes[key]
        chunks = script[min(idx, len(script) - 1)]

        async def _gen():
            for chunk in chunks:
                yield chunk

        return _gen()


@pytest.mark.asyncio
async def test_coreloop_orchestration_spawn_wait_over_rpc() -> None:
    """P3.1 sidecar exposure: params.orchestration advertises the four
    tools; the parent drives a child to completion; lifecycle events land
    as agent.child notifications."""
    provider = _ContentRoutedProvider(
        {
            "parent-turn": [
                _tool_round(
                    ToolCall(id="s1", name="agent_spawn", arguments={"task": "child-turn"})
                ),
                _tool_round(
                    ToolCall(id="w1", name="agent_wait", arguments={"childId": "0.1"})
                ),
                _text_round("all done"),
            ],
            "child-turn": [_text_round("child answer")],
        }
    )
    sidecar = _make_sidecar(provider)

    stream_id, events = await _run_stream(
        sidecar,
        {
            "provider": "openai_compat",
            "model": "fake",
            "messages": [{"role": "user", "content": "parent-turn"}],
            "useCoreLoop": True,
            "orchestration": {"maxDepth": 1, "maxParallel": 2},
        },
    )
    # agent.child notifications are scheduled fire-and-forget; let them land.
    await asyncio.sleep(0.05)

    done = [p for m, p in events if m == "stream.done"]
    assert len(done) == 1
    assert done[0]["ok"] is True
    assert done[0]["status"] == "completed"

    # The parent's first request advertised the orchestration tool family.
    first_tools = provider.stream_kwargs[0].get("tools") or []
    names = {t["function"]["name"] for t in first_tools}
    assert {"agent_spawn", "agent_send", "agent_wait", "agent_close"} <= names

    child_events = [p for m, p in events if m == "agent.child"]
    kinds = [p["kind"] for p in child_events]
    assert "child_spawned" in kinds
    assert "child_completed" in kinds
    spawned = next(p for p in child_events if p["kind"] == "child_spawned")
    assert spawned["childId"] == "0.1"
    assert spawned["streamId"] == stream_id


# ─── W2.8.2: systemPrompt as a typed fragment param ──────────────────────────


@pytest.mark.asyncio
async def test_system_prompt_param_lands_as_first_message() -> None:
    provider = _ScriptedProvider([_text_round("ok")])
    sidecar = _make_sidecar(provider)

    await _run_stream(
        sidecar,
        {
            "provider": "openai_compat",
            "model": "fake",
            "systemPrompt": "你是桌面助手的系统提示词。",
            "messages": [{"role": "user", "content": "hi"}],
            "useCoreLoop": True,
        },
    )

    first = provider.seen_messages[0]
    assert first[0].role == "system"
    assert first[0].content_text == "你是桌面助手的系统提示词。"
    assert first[1].role == "user"


@pytest.mark.asyncio
async def test_system_prompt_param_and_system_message_conflict() -> None:
    provider = _ScriptedProvider([_text_round("ok")])
    sidecar = _make_sidecar(provider)

    response = await sidecar.server.handle_frame(
        _frame(
            "agent.chat.stream",
            {
                "provider": "openai_compat",
                "model": "fake",
                "systemPrompt": "param prompt",
                "messages": [
                    {"role": "system", "content": "seed prompt"},
                    {"role": "user", "content": "hi"},
                ],
                "useCoreLoop": True,
            },
        )
    )
    assert "error" in response
    assert "mutually exclusive" in response["error"]["message"]


@pytest.mark.asyncio
async def test_system_prompt_over_cap_is_degraded() -> None:
    provider = _ScriptedProvider([_text_round("ok")])
    sidecar = _make_sidecar(provider)

    # ~5 chars/token estimate → 30k chars comfortably crosses the 4096 cap.
    huge = "规则。\n" * 10_000
    await _run_stream(
        sidecar,
        {
            "provider": "openai_compat",
            "model": "fake",
            "systemPrompt": huge,
            "messages": [{"role": "user", "content": "hi"}],
            "useCoreLoop": True,
        },
    )

    first = provider.seen_messages[0]
    assert first[0].role == "system"
    assert len(first[0].content_text) < len(huge)
    assert "fragment truncated" in first[0].content_text
