"""Direct unit tests for the JSON-RPC method handlers (no real stdio)."""

from __future__ import annotations

import json

import pytest

from steerable_sidecar import Sidecar


@pytest.fixture
def sidecar() -> Sidecar:
    return Sidecar()


async def _call(sidecar: Sidecar, method: str, params: dict | None = None, request_id: int = 1):
    raw = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    return await sidecar.server.handle_frame(raw)


async def test_system_ping_returns_health(sidecar: Sidecar) -> None:
    response = await _call(sidecar, "system.ping")
    assert response["result"]["version"] == "0.1.0"
    assert response["result"]["protocolVersion"] == "0.1.0"
    assert response["result"]["loadedTools"] == 0


async def test_session_create_then_resume(sidecar: Sidecar) -> None:
    create = await _call(
        sidecar,
        "agent.session.create",
        {"chatId": "chat-1", "userId": "user-1"},
    )
    session_id = create["result"]["sessionId"]
    resume = await _call(sidecar, "agent.session.resume", {"sessionId": session_id})
    assert resume["result"]["sessionId"] == session_id
    assert resume["result"]["chatId"] == "chat-1"


async def test_session_list_filters_by_user(sidecar: Sidecar) -> None:
    await _call(sidecar, "agent.session.create", {"chatId": "c1", "userId": "u1"})
    await _call(sidecar, "agent.session.create", {"chatId": "c2", "userId": "u2"})
    listed = await _call(sidecar, "agent.session.list", {"userId": "u1"})
    assert len(listed["result"]) == 1
    assert listed["result"][0]["userId"] == "u1"


async def test_tool_invoke_runs_registered_handler(sidecar: Sidecar) -> None:
    async def echo(value: str = "default") -> dict:
        return {"echoed": value}

    sidecar.tools.register(echo)
    response = await _call(
        sidecar,
        "tool.invoke",
        {"name": "echo", "arguments": {"value": "hi"}},
    )
    assert response["result"]["success"] is True
    assert response["result"]["data"]["value"] == {"echoed": "hi"}


async def test_tool_invoke_missing_tool_returns_failure() -> None:
    sidecar = Sidecar()
    response = await _call(
        sidecar,
        "tool.invoke",
        {"name": "missing"},
    )
    # Unknown tool is reported as ToolResult success=False, not as JSON-RPC error.
    assert response["result"]["success"] is False


async def test_tool_invoke_destructive_requires_consent() -> None:
    sidecar = Sidecar()

    async def delete_thing() -> None:
        return None

    sidecar.tools.register(delete_thing)
    denied = await _call(sidecar, "tool.invoke", {"name": "delete_thing"})
    assert denied["error"]["kind"] == "policy_denied"
    granted = await _call(
        sidecar,
        "tool.invoke",
        {"name": "delete_thing", "consentGranted": True},
    )
    assert granted["result"]["success"] is True


async def test_unknown_method_returns_not_found(sidecar: Sidecar) -> None:
    response = await _call(sidecar, "agent.nope")
    assert response["error"]["kind"] == "method_not_found"


async def test_invalid_params_object_returns_invalid_params(sidecar: Sidecar) -> None:
    response = await _call(sidecar, "agent.session.create", None)
    assert response["error"]["kind"] == "invalid_params"


async def test_resume_unknown_session_returns_invalid_request(sidecar: Sidecar) -> None:
    response = await _call(sidecar, "agent.session.resume", {"sessionId": "nope"})
    assert response["error"]["kind"] == "invalid_request"


async def test_config_get_set_round_trip(sidecar: Sidecar) -> None:
    initial = await _call(sidecar, "config.get")
    assert initial["result"]["logLevel"] == "INFO"
    await _call(sidecar, "config.set", {"logLevel": "DEBUG"})
    after = await _call(sidecar, "config.get")
    assert after["result"]["logLevel"] == "DEBUG"


async def test_health_snapshot_includes_pid_and_python(sidecar: Sidecar) -> None:
    health = await sidecar.snapshot_health()
    assert health.pid is not None
    assert health.pythonVersion is not None
    assert health.platform is not None


# ---------------------------------------------------------------------------
# agent.chat.stream
# ---------------------------------------------------------------------------


class _FakeProvider:
    """Pure-Python LLMProvider used by the chat-stream tests.

    Implements just enough of the Protocol surface (``stream``) and feeds a
    deterministic sequence of chunks so we can assert the sidecar transport
    behavior without touching a real network."""

    name = "fake"
    model = "fake-model"

    def __init__(self, *, chunks=None, raise_on=None):
        from steerable_agent_runtime.llm import LLMStreamChunk, LLMUsage

        self._chunks = chunks or [
            LLMStreamChunk(content_delta="Hello "),
            LLMStreamChunk(content_delta="world"),
            LLMStreamChunk(
                finish_reason="stop",
                usage=LLMUsage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
            ),
        ]
        self._raise_on = raise_on

    async def complete(self, *args, **kwargs):
        raise NotImplementedError

    def stream(self, messages, **kwargs):
        async def _gen():
            for i, chunk in enumerate(self._chunks):
                if self._raise_on is not None and i == self._raise_on:
                    raise RuntimeError("upstream blew up")
                yield chunk

        return _gen()


class _CapturingTransport:
    """Drop-in for StdioJsonRpcTransport that just buffers notifications."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit_notification(self, method: str, params: dict | None = None) -> None:
        self.events.append((method, params or {}))

    async def aclose(self) -> None:
        return None


@pytest.fixture
def sidecar_with_fake_llm():
    sidecar = Sidecar(llm_provider_factory=lambda params: _FakeProvider())
    transport = _CapturingTransport()
    sidecar._transport = transport  # type: ignore[attr-defined]
    return sidecar, transport


async def test_chat_stream_emits_chunks_then_done(sidecar_with_fake_llm) -> None:
    sidecar, transport = sidecar_with_fake_llm
    response = await _call(
        sidecar,
        "agent.chat.stream",
        {
            "provider": "openai_compat",
            "model": "fake-model",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    stream_id = response["result"]["streamId"]
    assert stream_id.startswith("str_")

    # Wait for the stream task to finish so all notifications are flushed.
    task = sidecar._streams.get(stream_id) or next(iter(sidecar._streams.values()), None)
    if task is not None:
        await task

    methods = [name for name, _ in transport.events]
    assert methods.count("stream.chunk") == 3
    assert methods.count("stream.done") == 1
    assert methods[-1] == "stream.done"
    assert transport.events[-1][1] == {"streamId": stream_id, "ok": True}
    # finish_reason / usage propagate on the last chunk.
    last_chunk = transport.events[-2][1]
    assert last_chunk["finishReason"] == "stop"
    assert last_chunk["usage"]["totalTokens"] == 12


async def test_chat_stream_invalid_provider_returns_invalid_params() -> None:
    def factory(_params):
        raise ValueError("nope")

    sidecar = Sidecar(llm_provider_factory=factory)
    sidecar._transport = _CapturingTransport()  # type: ignore[attr-defined]
    response = await _call(
        sidecar,
        "agent.chat.stream",
        {"provider": "bogus", "model": "x", "messages": []},
    )
    assert response["error"]["kind"] == "invalid_params"


async def test_chat_stream_invalid_message_role_rejected() -> None:
    sidecar = Sidecar(llm_provider_factory=lambda params: _FakeProvider())
    sidecar._transport = _CapturingTransport()  # type: ignore[attr-defined]
    response = await _call(
        sidecar,
        "agent.chat.stream",
        {
            "provider": "openai_compat",
            "model": "x",
            "messages": [{"role": "wizard", "content": "spell"}],
        },
    )
    assert response["error"]["kind"] == "invalid_params"


async def test_chat_stream_emits_error_on_provider_failure() -> None:
    sidecar = Sidecar(
        llm_provider_factory=lambda params: _FakeProvider(raise_on=1),
    )
    transport = _CapturingTransport()
    sidecar._transport = transport  # type: ignore[attr-defined]
    response = await _call(
        sidecar,
        "agent.chat.stream",
        {
            "provider": "openai_compat",
            "model": "fake-model",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    stream_id = response["result"]["streamId"]
    task = sidecar._streams.get(stream_id) or next(iter(sidecar._streams.values()), None)
    if task is not None:
        await task

    methods = [name for name, _ in transport.events]
    assert "stream.error" in methods
    err_payload = next(p for n, p in transport.events if n == "stream.error")
    assert err_payload["streamId"] == stream_id
    assert err_payload["kind"] == "RuntimeError"


async def test_chat_cancel_terminates_in_flight_stream() -> None:
    import asyncio as _asyncio

    from steerable_agent_runtime.llm import LLMStreamChunk

    class _SlowProvider:
        name = "slow"
        model = "slow-model"

        async def complete(self, *a, **k):
            raise NotImplementedError

        def stream(self, messages, **kwargs):
            async def _gen():
                yield LLMStreamChunk(content_delta="first")
                # Block long enough that cancel can land.
                await _asyncio.sleep(5.0)
                yield LLMStreamChunk(content_delta="never")

            return _gen()

    sidecar = Sidecar(llm_provider_factory=lambda params: _SlowProvider())
    transport = _CapturingTransport()
    sidecar._transport = transport  # type: ignore[attr-defined]

    response = await _call(
        sidecar,
        "agent.chat.stream",
        {
            "provider": "openai_compat",
            "model": "slow-model",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    stream_id = response["result"]["streamId"]
    # Give the task one tick to start streaming.
    await _asyncio.sleep(0)

    cancel_response = await _call(sidecar, "agent.chat.cancel", {"streamId": stream_id})
    # Successful void calls drop the `result` key via model_dump(exclude_none=True).
    assert "error" not in cancel_response

    # Drain any leftover task (it should have been cancelled).
    leftover = sidecar._streams.get(stream_id)
    if leftover is not None:
        with pytest.raises(_asyncio.CancelledError):
            await leftover

    cancel_done = next(
        (p for n, p in transport.events if n == "stream.done" and p.get("cancelled")),
        None,
    )
    # Either a cancelled-stream.done was emitted, or the task was cancelled
    # before the finally block ran. Both are acceptable; we just ensure no
    # `never` chunk leaked.
    chunk_deltas = [p.get("delta") for n, p in transport.events if n == "stream.chunk"]
    assert "never" not in chunk_deltas
    if cancel_done is not None:
        assert cancel_done["streamId"] == stream_id
