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
    """Captures session/update notifications like an editor would.

    Answers ``session/request_permission`` with allow-once, like an
    editor whose user approves everything; permission tests override this.
    """

    def __init__(self):
        self.updates: list[tuple[str, object]] = []
        self.permission_requests: list[tuple[str, object, list]] = []

    async def session_update(self, session_id: str, update, **kwargs) -> None:
        self.updates.append((session_id, update))

    async def request_permission(self, session_id: str, tool_call, options, **kwargs):
        from acp.schema import AllowedOutcome, RequestPermissionResponse

        self.permission_requests.append((session_id, tool_call, options))
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id="allow-once")
        )


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
    # W3.4.1.1: session lifecycle is served (list/load/resume/fork).
    assert resp.agent_capabilities.load_session is True


@pytest.mark.asyncio
async def test_list_load_resume_session_lifecycle() -> None:
    """W3.4.1.1: new_session persists a row; list sees it; load/resume
    hydrate the host view from the durable record."""
    agent, _ = _agent(_ScriptedProvider([_text_round("Hello back")]))
    session = await agent.new_session(cwd="/tmp")
    await agent.prompt(session.session_id, _prompt("hello"))

    listed = await agent.list_sessions()
    ids = [s.session_id for s in listed.sessions]
    assert session.session_id in ids
    info = next(s for s in listed.sessions if s.session_id == session.session_id)
    assert info.cwd == "/tmp"

    # Simulate a restart: drop the live session, load from the record.
    agent._sessions.clear()
    await agent.load_session(cwd="/tmp", session_id=session.session_id)
    hydrated = agent._sessions[session.session_id]
    texts = [
        "".join(p.text for p in m.content if getattr(p, "type", None) == "text")
        for m in hydrated.history
    ]
    assert "hello" in texts
    assert "Hello back" in texts

    agent._sessions.clear()
    await agent.resume_session(session_id=session.session_id, cwd="/tmp")
    assert session.session_id in agent._sessions


@pytest.mark.asyncio
async def test_load_unknown_session_fails_loud() -> None:
    import acp

    agent, _ = _agent(_ScriptedProvider([_text_round("hi")]))
    with pytest.raises(acp.RequestError):
        await agent.load_session(cwd="/tmp", session_id="no-such-session")


@pytest.mark.asyncio
async def test_fork_session_branches_the_record() -> None:
    """W3.4.1.2: fork over standard ACP — the branch-family differentiator."""
    agent, _ = _agent(_ScriptedProvider([_text_round("original reply")]))
    session = await agent.new_session(cwd="/tmp")
    await agent.prompt(session.session_id, _prompt("hello"))

    forked = await agent.fork_session(session_id=session.session_id, cwd="/tmp")
    assert forked.session_id != session.session_id
    branch = agent._sessions[forked.session_id]
    texts = [
        "".join(p.text for p in m.content if getattr(p, "type", None) == "text")
        for m in branch.history
    ]
    assert "hello" in texts  # seed carried over

    # The branch is enumerable and the source record is untouched.
    listed = await agent.list_sessions()
    ids = [s.session_id for s in listed.sessions]
    assert forked.session_id in ids and session.session_id in ids


# -- W3.4.2: session config, modes, MCP mounting ------------------------------


@pytest.mark.asyncio
async def test_new_session_advertises_modes() -> None:
    agent, _ = _agent(_ScriptedProvider([_text_round("hi")]))
    resp = await agent.new_session(cwd="/tmp")
    assert resp.modes.current_mode_id == "default"
    assert {m.id for m in resp.modes.available_modes} == {"default", "read-only"}


@pytest.mark.asyncio
async def test_set_session_mode_read_only_denies_writes() -> None:
    import acp

    router = ToolRouter()

    async def _write(path: str, content: str) -> dict:
        return {"success": True, "data": {"written": path}}

    router.register(
        _write, name="write_file", mode="safe_write",
        description="w", schema={"type": "object"},
    )
    provider = _ScriptedProvider(
        [
            [
                LLMStreamChunk(
                    tool_call_delta=ToolCall(
                        id="c1", name="write_file",
                        arguments={"path": "a", "content": "b"},
                    )
                ),
                LLMStreamChunk(
                    finish_reason="tool_calls",
                    usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                ),
            ],
            _text_round("blocked"),
        ]
    )
    agent, _ = _agent(provider, tools=router)
    session = await agent.new_session(cwd="/tmp")
    await agent.set_session_mode(session.session_id, "read-only")
    resp = await agent.prompt(session.session_id, _prompt("write something"))
    assert resp.stop_reason == "end_turn"
    # The write never executed: the mode gate denied it before approval.
    with pytest.raises(acp.RequestError):
        await agent.set_session_mode(session.session_id, "plan")  # unknown mode


@pytest.mark.asyncio
async def test_set_config_option_overrides_provider_params() -> None:
    captured: list[dict] = []

    def _factory(params):
        captured.append(dict(params))
        return _ScriptedProvider([_text_round("ok")])

    agent = SteerableAcpAgent(
        provider_params={"provider": "openai_compat", "model": "env-model"},
        llm_provider_factory=_factory,
        tools=ToolRouter(),
    )
    session = await agent.new_session(cwd="/tmp")
    await agent.set_config_option("model", session.session_id, "session-model")
    await agent.prompt(session.session_id, _prompt("hi"))
    assert captured[-1]["model"] == "session-model"

    import acp

    with pytest.raises(acp.RequestError):
        await agent.set_config_option("apiKey", session.session_id, "sk-nope")


@pytest.mark.asyncio
async def test_new_session_rejects_non_stdio_mcp() -> None:
    """W3.4.2.1: HTTP/SSE MCP servers are an honest gap — fail loud."""
    import acp
    from acp.schema import HttpMcpServer

    agent, _ = _agent(_ScriptedProvider([_text_round("hi")]))
    with pytest.raises(acp.RequestError):
        await agent.new_session(
            cwd="/tmp",
            mcp_servers=[
                HttpMcpServer(
                    name="web", url="https://mcp.example.test/", headers=[], type="http"
                )
            ],
        )


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
async def test_destructive_tool_triggers_permission_and_executes_on_allow() -> None:
    """P0: the ACP path must not hardcode consent — a destructive tool asks
    the editor, and an allow verdict executes the call."""
    tools = ToolRouter()
    ran: list[str] = []

    async def wipe() -> ToolResult:
        ran.append("wiped")
        return ToolResult(success=True, data={"ok": True})

    tools.register(wipe, name="wipe", mode="destructive")
    provider = _ScriptedProvider(
        [
            [
                LLMStreamChunk(
                    tool_call_delta=ToolCall(id="c1", name="wipe", arguments={})
                ),
                LLMStreamChunk(
                    finish_reason="tool_calls",
                    usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                ),
            ],
            _text_round("done"),
        ]
    )
    agent, client = _agent(provider, tools)
    session = await agent.new_session(cwd="/tmp")

    resp = await agent.prompt(session.session_id, _prompt("wipe it"))

    assert resp.stop_reason == "end_turn"
    assert ran == ["wiped"]
    assert len(client.permission_requests) == 1
    _, tool_call, options = client.permission_requests[0]
    assert tool_call.tool_call_id == "c1"
    assert tool_call.title == "wipe"
    assert [o.kind for o in options] == [
        "allow_once",
        "allow_always",
        "reject_once",
        "reject_always",
    ]


@pytest.mark.asyncio
async def test_destructive_tool_denied_is_not_executed() -> None:
    """P0 regression: rejecting the permission prompt blocks execution and
    the denial reaches the model as a tool result, not a hang."""
    from acp.schema import DeniedOutcome, RequestPermissionResponse

    class _DenyingClient(_FakeClient):
        async def request_permission(self, session_id, tool_call, options, **kwargs):
            self.permission_requests.append((session_id, tool_call, options))
            return RequestPermissionResponse(
                outcome=DeniedOutcome(outcome="cancelled")
            )

    tools = ToolRouter()
    ran: list[str] = []

    async def wipe() -> ToolResult:
        ran.append("wiped")
        return ToolResult(success=True, data={"ok": True})

    tools.register(wipe, name="wipe", mode="destructive")
    provider = _ScriptedProvider(
        [
            [
                LLMStreamChunk(
                    tool_call_delta=ToolCall(id="c1", name="wipe", arguments={})
                ),
                LLMStreamChunk(
                    finish_reason="tool_calls",
                    usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                ),
            ],
            _text_round("understood, not wiping"),
        ]
    )
    agent = SteerableAcpAgent(
        provider_params={"provider": "openai_compat", "model": "fake"},
        llm_provider_factory=lambda _params: provider,
        tools=tools,
    )
    client = _DenyingClient()
    agent.on_connect(client)
    session = await agent.new_session(cwd="/tmp")

    resp = await agent.prompt(session.session_id, _prompt("wipe it"))

    assert resp.stop_reason == "end_turn"
    assert ran == []
    assert len(client.permission_requests) == 1
    from acp.schema import ToolCallProgress

    progresses = [u for _, u in client.updates if isinstance(u, ToolCallProgress)]
    assert progresses[0].status == "failed"


@pytest.mark.asyncio
async def test_read_tool_does_not_prompt() -> None:
    """Read-mode calls auto-approve: editors never gate reads, so no
    permission request must reach the client."""
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
    assert client.permission_requests == []


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


def test_env_provider_params_catalog_env_vars(monkeypatch) -> None:
    """W5.3.1: provider's catalog env names fill the key when the generic
    shim vars are absent."""
    from steerable_sidecar.acp_adapter import _env_provider_params

    for var in ("STEERABLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("STEERABLE_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    params = _env_provider_params()
    assert params["apiKey"] == "sk-deepseek"


def test_factory_fills_base_url_from_catalog() -> None:
    """W5.3.1: a catalogued provider kind needs no hand-written base_url."""
    from steerable_sidecar.sidecar import _catalog_base_url

    assert _catalog_base_url("deepseek") == "https://api.deepseek.com"
    assert _catalog_base_url("openrouter") == "https://openrouter.ai/api/v1"
    # First-party default: models.dev omits the field, factory keeps its
    # hardcoded https://api.openai.com/v1 fallback.
    assert _catalog_base_url("openai") is None
    # Compat shims are not catalog providers.
    assert _catalog_base_url("openai_compat") is None


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
