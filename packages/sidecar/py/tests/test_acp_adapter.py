"""Wave 3: ACP transport — the CoreLoop served as an acp.Agent."""

from __future__ import annotations

import asyncio

import pytest
from acp.schema import AgentMessageChunk, AgentThoughtChunk, TextContentBlock
from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime import ToolRouter
from steerable_agent_runtime.llm import LLMStreamChunk, LLMUsage

from steerable_sidecar.acp_adapter import SteerableAcpAgent


class _ScriptedProvider:
    name = "scripted"
    model = "scripted-model"

    def __init__(self, script, fail_on: set[int] | None = None):
        self._script = script
        self._fail_on = fail_on or set()
        self._round = 0
        self.calls: list[list] = []
        self.attempts = 0

    async def complete(self, *args, **kwargs):
        raise NotImplementedError

    def stream(self, messages, **kwargs):
        self.attempts += 1
        attempt = self.attempts
        self.calls.append(list(messages))
        chunks = self._script[min(self._round, len(self._script) - 1)]

        async def _gen():
            if attempt in self._fail_on:
                from steerable_agent_runtime.llm.errors import LLMError

                raise LLMError("upstream reset", kind="transport", provider="scripted")
                yield  # pragma: no cover — make this a generator
            self._round += 1
            for chunk in chunks:
                yield chunk

        return _gen()


def _text_round(text: str):
    return [
        LLMStreamChunk(content_delta=text),
        LLMStreamChunk(
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        ),
    ]


class _FakeClient:
    """Captures session/update notifications like an editor would."""

    def __init__(self):
        self.updates: list[tuple[str, object]] = []

    async def session_update(self, session_id: str, update, **kwargs) -> None:
        self.updates.append((session_id, update))


def _agent(provider, tools: ToolRouter | None = None):
    agent = SteerableAcpAgent(
        provider_params={"provider": "openai_compat", "model": "fake"},
        llm_provider_factory=lambda _params: provider,
        tools=tools,
    )
    client = _FakeClient()
    agent.on_connect(client)
    return agent, client


def _prompt(text: str):
    return [TextContentBlock(type="text", text=text)]


@pytest.mark.asyncio
async def test_initialize_advertises_text_only_capabilities() -> None:
    agent, _ = _agent(_ScriptedProvider([_text_round("hi")]))
    resp = await agent.initialize(protocol_version=1)
    assert resp.agent_info.name == "steerable-sidecar"
    assert resp.agent_capabilities.prompt_capabilities.image is False
    assert resp.agent_capabilities.load_session is False


@pytest.mark.asyncio
async def test_prompt_streams_text_and_ends_turn() -> None:
    agent, client = _agent(_ScriptedProvider([_text_round("Hello back")]))
    session = await agent.new_session(cwd="/tmp")

    resp = await agent.prompt(session.session_id, _prompt("hello"))

    assert resp.stop_reason == "end_turn"
    chunks = [u for sid, u in client.updates if sid == session.session_id]
    text = "".join(
        u.content.text for u in chunks if isinstance(u, AgentMessageChunk)
    )
    assert text == "Hello back"


@pytest.mark.asyncio
async def test_prompt_retries_transient_stream_error() -> None:
    provider = _ScriptedProvider([_text_round("Hello back")], fail_on={1})
    agent, client = _agent(provider)
    session = await agent.new_session(cwd="/tmp")

    resp = await agent.prompt(session.session_id, _prompt("hello"))

    assert resp.stop_reason == "end_turn"
    assert provider.attempts == 2
    chunks = [u for sid, u in client.updates if sid == session.session_id]
    text = "".join(
        u.content.text for u in chunks if isinstance(u, AgentMessageChunk)
    )
    assert text == "Hello back"


@pytest.mark.asyncio
async def test_prompt_forwards_tool_calls() -> None:
    tools = ToolRouter()

    async def get_time() -> ToolResult:
        return ToolResult(success=True, data={"time": "noon"})

    tools.register(get_time, name="get_time", mode="read", concurrency_safe=True)
    provider = _ScriptedProvider(
        [
            [
                LLMStreamChunk(
                    tool_call_delta=ToolCall(id="c1", name="get_time", arguments={})
                ),
                LLMStreamChunk(
                    finish_reason="tool_calls",
                    usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                ),
            ],
            _text_round("noon it is"),
        ]
    )
    agent, client = _agent(provider, tools)
    session = await agent.new_session(cwd="/tmp")

    resp = await agent.prompt(session.session_id, _prompt("time?"))

    assert resp.stop_reason == "end_turn"
    from acp.schema import ToolCallProgress, ToolCallStart

    starts = [u for _, u in client.updates if isinstance(u, ToolCallStart)]
    progresses = [u for _, u in client.updates if isinstance(u, ToolCallProgress)]
    assert [s.title for s in starts] == ["get_time"]
    assert starts[0].tool_call_id == "c1"
    assert progresses[0].status == "completed"


@pytest.mark.asyncio
async def test_unknown_session_raises() -> None:
    agent, _ = _agent(_ScriptedProvider([_text_round("hi")]))
    import acp

    with pytest.raises(acp.RequestError):
        await agent.prompt("no-such-session", _prompt("hello"))


@pytest.mark.asyncio
async def test_cancel_turns_into_cancelled_stop_reason() -> None:
    never = asyncio.Event()

    class _SlowProvider(_ScriptedProvider):
        def stream(self, messages, **kwargs):
            async def _gen():
                yield LLMStreamChunk(content_delta="partial")
                await never.wait()

            return _gen()

    agent, client = _agent(_SlowProvider([]))
    session = await agent.new_session(cwd="/tmp")

    task = asyncio.ensure_future(agent.prompt(session.session_id, _prompt("go")))
    await asyncio.sleep(0.05)  # let the first chunk stream
    await agent.cancel(session.session_id)
    resp = await task

    assert resp.stop_reason == "cancelled"


@pytest.mark.asyncio
async def test_second_turn_seeds_from_the_record() -> None:
    """Multi-turn: turn 2's provider call must contain turn 1's messages —
    the loop's record-aware seeding, not the adapter re-sending history."""
    provider = _ScriptedProvider([_text_round("first answer"), _text_round("second")])
    agent, _ = _agent(provider)
    session = await agent.new_session(cwd="/tmp")

    await agent.prompt(session.session_id, _prompt("question one"))
    await agent.prompt(session.session_id, _prompt("follow up"))

    assert len(provider.calls) == 2
    turn2_messages = provider.calls[1]
    roles = [m.role for m in turn2_messages]
    contents = [
        getattr(p, "text", "") for m in turn2_messages for p in m.content
    ]
    # Turn 1's user + assistant are in the seed, then the new user message.
    assert roles == ["user", "assistant", "user"]
    assert "question one" in contents[0]
    assert "first answer" in contents[1]
    assert "follow up" in contents[2]


@pytest.mark.asyncio
async def test_default_workspace_bash(tmp_path, monkeypatch) -> None:
    provider = _ScriptedProvider(
        [
            [
                LLMStreamChunk(
                    tool_call_delta=ToolCall(
                        id="c1", name="bash", arguments={"command": "echo hi"}
                    )
                ),
                LLMStreamChunk(
                    finish_reason="tool_calls",
                    usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                ),
            ],
            _text_round("said hi"),
        ]
    )
    agent, client = _agent(provider)
    session = await agent.new_session(cwd=str(tmp_path))
    resp = await agent.prompt(session.session_id, _prompt("echo"))
    assert resp.stop_reason == "end_turn"
    from acp.schema import ToolCallStart

    starts = [u for _, u in client.updates if isinstance(u, ToolCallStart)]
    assert [s.title for s in starts] == ["bash"]


@pytest.mark.asyncio
async def test_close_session_and_in_flight() -> None:
    import acp

    never = asyncio.Event()

    class _SlowProvider(_ScriptedProvider):
        def stream(self, messages, **kwargs):
            async def _gen():
                yield LLMStreamChunk(content_delta="partial")
                await never.wait()

            return _gen()

    agent, _ = _agent(_SlowProvider([]))
    session = await agent.new_session(cwd="/tmp")
    task = asyncio.ensure_future(agent.prompt(session.session_id, _prompt("go")))
    await asyncio.sleep(0.05)
    with pytest.raises(acp.RequestError):
        await agent.prompt(session.session_id, _prompt("again"))
    await agent.close_session(session.session_id)
    resp = await task
    assert resp.stop_reason == "cancelled"


def test_env_provider_params_fallbacks(monkeypatch) -> None:
    from steerable_sidecar.acp_adapter import _env_provider_params

    monkeypatch.delenv("STEERABLE_API_KEY", raising=False)
    monkeypatch.delenv("STEERABLE_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("STEERABLE_MODEL", "gpt-5.5")
    params = _env_provider_params()
    assert params["apiKey"] == "sk-openai"
    assert params["baseUrl"] == "https://example.test/v1"
    assert params["model"] == "gpt-5.5"


def test_acp_main_serves_stdio(monkeypatch) -> None:
    from steerable_sidecar import acp_adapter

    called = {}

    def _run_agent(agent) -> None:
        called["agent"] = agent

    monkeypatch.setattr(acp_adapter.acp, "run_agent", _run_agent)
    acp_adapter.main()
    assert called["agent"].__class__.__name__ == "SteerableAcpAgent"


@pytest.mark.asyncio
async def test_failed_completion_surfaces_reason_and_ends() -> None:
    class _FailingProvider(_ScriptedProvider):
        def stream(self, messages, **kwargs):
            async def _gen():
                raise RuntimeError("provider exploded")
                yield  # pragma: no cover

            return _gen()

    agent, client = _agent(_FailingProvider([]))
    session = await agent.new_session(cwd="/tmp")

    resp = await agent.prompt(session.session_id, _prompt("go"))

    assert resp.stop_reason == "end_turn"
    text = "".join(
        u.content.text
        for _, u in client.updates
        if isinstance(u, AgentMessageChunk)
    )
    assert "provider exploded" in text
