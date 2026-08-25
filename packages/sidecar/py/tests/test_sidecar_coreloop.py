"""agent.chat.stream via CoreLoop (useCoreLoop flag / STEERABLE_SIDECAR_CORELOOP)."""

from __future__ import annotations

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

    async def complete(self, *args, **kwargs):
        raise NotImplementedError

    def stream(self, messages, **kwargs):
        self.attempts += 1
        attempt = self.attempts
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
    assert tool_calls == [{"id": "c1", "name": "add"}]
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
