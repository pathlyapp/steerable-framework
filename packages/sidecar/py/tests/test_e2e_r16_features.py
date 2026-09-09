"""E2E: R16 feature pass through real sidecar processes.

One real-process scenario per R16 deliverable that did not already own an
e2e module (ACP: test_acp_e2e.py; run_code sandbox: test_e2e_run_code.py;
JS PTC: test_e2e_ptc_js.py; web_fetch/Tavily: test_e2e_web_tools.py;
progressive tools: test_e2e_tool_exposure.py). Covered here, all over a
real stdio JSON-RPC sidecar with the LLM on the loopback mock:

- ask_user CC-schema normalization and the ask_user.request reverse channel
- delegate_subagent named profiles (subagent_type enum) + agent.child stream
- agent.chat.compact (compact_now) folding old tool results mid-turn
- micro-compaction via the harness spec (helper sidecar)
- stream.chunk delta notifications and the streamRawChunks raw channel
- Brave web-search provider against a loopback Brave endpoint
- session-tree: fork fan-out then agent.session.tree cousin resolution
- provider presets: auto-match / off / pinned override on the wire
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from e2e_harness import (
    TESTS_DIR,
    SidecarClient,
    child_env,
    model_tool_names,
    sse_text,
    sse_tool_call,
)

pytestmark = pytest.mark.usefixtures("e2e_gate")


# ---------------------------------------------------------------------------
# Reverse-channel client: captures and answers sidecar -> host requests
# (the shared harness answers method_not_found, which ask_user/tool.invoke
# scenarios must not do).
# ---------------------------------------------------------------------------


class _HostClient(SidecarClient):
    """SidecarClient that serves ask_user.request and tool.invoke."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        super().__init__(proc)
        self.ask_user_payloads: list[dict[str, Any]] = []
        self.tool_invocations: list[dict[str, Any]] = []
        self.ask_user_reply: dict[str, Any] = {"answers": {}}
        #: (params) -> result payload; may be an async function.
        self.tool_invoke_handler: Any = None
        self._tool_event = asyncio.Event()

    def _route(self, payload: dict[str, Any]) -> None:
        if "method" in payload and "id" in payload:
            method = payload["method"]
            if method == "ask_user.request":
                self.ask_user_payloads.append(payload.get("params") or {})
                asyncio.ensure_future(
                    self._write(
                        {
                            "jsonrpc": "2.0",
                            "id": payload["id"],
                            "result": self.ask_user_reply,
                        }
                    )
                )
                return
            if method == "tool.invoke" and self.tool_invoke_handler is not None:
                self.tool_invocations.append(payload.get("params") or {})
                self._tool_event.set()
                asyncio.ensure_future(self._answer_tool_invoke(payload))
                return
        super()._route(payload)

    async def _answer_tool_invoke(self, payload: dict[str, Any]) -> None:
        result = self.tool_invoke_handler(payload.get("params") or {})
        if asyncio.iscoroutine(result):
            result = await result
        await self._write(
            {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        )

    async def wait_tool_invocations(self, count: int, timeout: float = 15.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self.tool_invocations) < count:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"expected {count} tool.invoke calls, saw "
                    f"{len(self.tool_invocations)}"
                )
            self._tool_event.clear()
            try:
                await asyncio.wait_for(self._tool_event.wait(), remaining)
            except asyncio.TimeoutError:
                continue


@pytest.fixture
async def host_client_factory(tmp_path: Path) -> Any:
    """Spawn the stock sidecar under the reverse-channel-capable client."""
    clients: list[_HostClient] = []

    async def spawn(env_overrides: dict[str, str | None] | None = None) -> _HostClient:
        client = await _HostClient.spawn(
            [sys.executable, "-m", "steerable_sidecar", "--log-level", "ERROR"],
            env=child_env(tmp_path, env_overrides),
        )
        clients.append(client)
        return client

    yield spawn
    for client in clients:
        await client.aclose()


def _stream_params(mock: Any, **extra: Any) -> dict[str, Any]:
    return {
        "provider": "openai_compat",
        "model": "mock-e2e",
        "baseUrl": mock.base_url,
        "apiKey": "e2e-not-a-real-key",
        "messages": [{"role": "user", "content": "hi"}],
        "useCoreLoop": True,
        **extra,
    }


async def _await_done(client: SidecarClient, stream_id: str) -> None:
    await client.wait_for_notification(
        "stream.done",
        predicate=lambda p: p.get("streamId") == stream_id,
        timeout=60.0,
    )


# ---------------------------------------------------------------------------
# R16 P1: ask_user CC-schema normalization over the reverse channel
# ---------------------------------------------------------------------------


async def test_ask_user_schema_enforced_and_normalized_over_reverse_channel(
    host_client_factory: Any, mock_openai: Any
) -> None:
    def responder(body: dict[str, Any], index: int) -> list[dict[str, Any]]:
        if index == 0:
            # Violation: 5 questions (bound is 1-4) — the tool result must
            # name the fix and the turn must go on.
            return sse_tool_call(
                "ask_user",
                {
                    "intro": "too many",
                    "questions": [
                        {"id": f"q{i}", "text": f"Question {i}?"}
                        for i in range(5)
                    ],
                },
                call_id="call_bad",
            )
        if index == 1:
            # Violation: multiSelect omitted — CC parity requires the model
            # to commit to single vs multi explicitly.
            return sse_tool_call(
                "ask_user",
                {
                    "intro": "pick one",
                    "questions": [
                        {
                            "id": "flavor",
                            "text": "Which flavor do you want?",
                            "type": "select",
                            "options": ["vanilla", "chocolate"],
                        }
                    ],
                },
                call_id="call_no_multiselect",
            )
        if index == 2:
            # Valid minimal select: no header — the reverse payload must
            # carry the derived header and the committed multiSelect.
            return sse_tool_call(
                "ask_user",
                {
                    "intro": "pick one",
                    "questions": [
                        {
                            "id": "flavor",
                            "text": "Which flavor do you want?",
                            "type": "select",
                            "options": ["vanilla", "chocolate"],
                            "multiSelect": False,
                        }
                    ],
                },
                call_id="call_good",
            )
        return sse_text("ASK_USER_E2E_OK")

    mock = mock_openai(responder)
    client = await host_client_factory()
    client.ask_user_reply = {"answers": {"flavor": "vanilla"}}

    result = await client.request(
        "agent.chat.stream", _stream_params(mock, askUser=True)
    )
    await _await_done(client, result["streamId"])

    # Both violations came back as tool results naming the fix…
    assert len(mock.requests) == 4
    tool_msgs_1 = [
        m for m in mock.requests[1]["messages"] if m.get("role") == "tool"
    ]
    assert any("1-4" in str(m.get("content")) for m in tool_msgs_1)
    tool_msgs_2 = [
        m for m in mock.requests[2]["messages"] if m.get("role") == "tool"
    ]
    assert any("multiSelect is required" in str(m.get("content")) for m in tool_msgs_2)

    # …and the retry reached the host normalized: derived header (<=12
    # chars), the committed multiSelect, options intact.
    assert len(client.ask_user_payloads) == 1
    question = client.ask_user_payloads[0]["questions"][0]
    assert question["header"] and len(question["header"]) <= 12
    assert question["multiSelect"] is False
    assert question["options"] == ["vanilla", "chocolate"]

    # The answer rode back and the turn completed with the final text.
    final = [
        p
        for p in client.notifications
        if p.get("method") == "stream.chunk"
        and (p.get("params") or {}).get("streamId") == result["streamId"]
    ]
    assert any("ASK_USER_E2E_OK" in str(p.get("params", {}).get("delta")) for p in final)


# ---------------------------------------------------------------------------
# R16 P1+P4: delegate_subagent named profiles on the agent pool
# ---------------------------------------------------------------------------


async def test_delegate_subagent_named_profile_runs_on_pool(
    host_client_factory: Any, mock_openai: Any
) -> None:
    CHILD_INSTRUCTION = "research the repository layout"

    def responder(body: dict[str, Any], index: int) -> list[dict[str, Any]]:
        messages = body.get("messages") or []
        last_user = next(
            (m for m in reversed(messages) if m.get("role") == "user"), {}
        )
        if last_user.get("content") == CHILD_INSTRUCTION:
            return sse_text("CHILD_DONE")
        if index == 0:
            return sse_tool_call(
                "delegate_subagent",
                {"task": CHILD_INSTRUCTION, "subagent_type": "researcher"},
                call_id="call_delegate",
            )
        return sse_text("PARENT_DONE")

    mock = mock_openai(responder)
    client = await host_client_factory()
    result = await client.request(
        "agent.chat.stream",
        _stream_params(
            mock,
            subagent={
                "profiles": {
                    "researcher": {
                        "description": "Read-only research agent",
                        "maxRounds": 2,
                    }
                }
            },
        ),
    )
    stream_id = result["streamId"]
    await _await_done(client, stream_id)

    # The schema advertised the profile name as a subagent_type enum.
    delegate = next(
        t
        for t in mock.requests[0]["tools"]
        if (t.get("function") or {}).get("name") == "delegate_subagent"
    )
    enum = (
        delegate["function"]["parameters"]["properties"]["subagent_type"]["enum"]
    )
    assert "researcher" in enum

    # The child ran its own provider request carrying the instruction…
    assert any(
        any(
            m.get("role") == "user" and m.get("content") == CHILD_INSTRUCTION
            for m in req.get("messages") or []
        )
        for req in mock.requests
    )
    # …its lifecycle landed on agent.child…
    child_kinds = [
        (p.get("params") or {}).get("kind")
        for p in client.notifications
        if p.get("method") == "agent.child"
        and (p.get("params") or {}).get("streamId") == stream_id
    ]
    assert child_kinds, "no agent.child notifications"
    # …and the parent turn completed.
    assert any(
        p.get("method") == "stream.done"
        and (p.get("params") or {}).get("ok") is True
        for p in client.notifications
    )


async def test_delegate_subagent_unknown_profile_fails_closed(
    host_client_factory: Any, mock_openai: Any
) -> None:
    def responder(_body: dict[str, Any], index: int) -> list[dict[str, Any]]:
        if index == 0:
            return sse_tool_call(
                "delegate_subagent",
                {"task": "x", "subagent_type": "ghost"},
                call_id="call_delegate",
            )
        return sse_text("RECOVERED")

    mock = mock_openai(responder)
    client = await host_client_factory()
    result = await client.request(
        "agent.chat.stream",
        _stream_params(
            mock, subagent={"profiles": {"researcher": {"description": "r"}}}
        ),
    )
    await _await_done(client, result["streamId"])

    # The unknown type failed closed, naming the registered profiles…
    tool_msgs = [
        m for m in mock.requests[1]["messages"] if m.get("role") == "tool"
    ]
    assert any("ghost" in str(m.get("content")) for m in tool_msgs)
    assert any("researcher" in str(m.get("content")) for m in tool_msgs)
    # …and no child ever started.
    assert not any(
        p.get("method") == "agent.child" for p in client.notifications
    )


# ---------------------------------------------------------------------------
# R16 P1: agent.chat.compact (compact_now) mid-turn
# ---------------------------------------------------------------------------


async def test_chat_compact_folds_old_tool_results_mid_turn(
    host_client_factory: Any, mock_openai: Any
) -> None:
    def responder(_body: dict[str, Any], index: int) -> list[dict[str, Any]]:
        if index < 3:
            return sse_tool_call(
                "bash", {"command": f"echo {index}"}, call_id=f"call_{index}"
            )
        return sse_text("COMPACT_E2E_OK")

    mock = mock_openai(responder)
    client = await host_client_factory()

    call_count = 0

    async def answer_tool(_params: dict[str, Any]) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            # Hold the third result so the compact RPC lands while the turn
            # is still inside tool execution; the loop consumes the request
            # at the next pre_step boundary.
            await asyncio.sleep(2.0)
        return {"output": f"tool output {call_count}"}

    client.tool_invoke_handler = answer_tool
    bash = {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
    # A small explicit window keeps keep_last_tool_results at the desktop
    # value (2) instead of the large-window 16, so three results fold one.
    result = await client.request(
        "agent.chat.stream",
        _stream_params(
            mock, tools=[bash], toolsViaHost=True, maxContextTokens=8192
        ),
    )
    stream_id = result["streamId"]

    await client.wait_tool_invocations(3)
    compact = await client.request("agent.chat.compact", {"streamId": stream_id})
    assert compact["ok"] is True
    await _await_done(client, stream_id)

    # keep_last_tool_results=2 on the small mock window: with three results
    # the oldest is folded in the request that follows the compact.
    final_request = mock.requests[3]
    tool_msgs = [m for m in final_request["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 3
    assert str(tool_msgs[0].get("content")).startswith(
        "[tool output folded to save context]"
    )
    # The two newest results stay readable.
    assert "tool output 2" in str(tool_msgs[1].get("content"))
    assert "tool output 3" in str(tool_msgs[2].get("content"))


async def test_micro_compaction_folds_on_interval_via_spec(
    e2e_gate: None, sidecar_factory: Any, mock_openai: Any, tmp_path: Path
) -> None:
    spec = {
        "context": [
            {
                "impl": "pressure_compaction",
                "params": {
                    # Huge window: pressure never fires; only the interval
                    # trigger is under test.
                    "max_context_tokens": 10_000_000,
                    "model": "mock-e2e",
                    "micro_compact_interval_rounds": 1,
                    "keep_last_tool_results": 1,
                },
            }
        ],
        "retry": ["none"],
        "validator": "null",
        "tools": "full",
        "memory": "stateless",
        "orchestration": "single",
    }
    spec_path = tmp_path / "micro.harness.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    def responder(_body: dict[str, Any], index: int) -> list[dict[str, Any]]:
        if index < 3:
            return sse_tool_call("echo", {"text": f"round {index}"}, call_id=f"e{index}")
        return sse_text("MICRO_E2E_OK")

    mock = mock_openai(responder)
    client = await sidecar_factory(
        [sys.executable, str(TESTS_DIR / "e2e_progressive_loop_sidecar.py")],
        wait_ready=False,
    )
    result = await client.request(
        "test.run_turn",
        {
            "specPath": str(spec_path),
            "provider": {
                "provider": "openai_compat",
                "model": "mock-e2e",
                "baseUrl": mock.base_url,
                "apiKey": "e2e-not-a-real-key",
            },
            "instruction": "echo three times",
        },
        timeout=60.0,
    )

    assert "MICRO_E2E_OK" in result["content"]
    micro = [
        e
        for e in result["events"]
        if e["kind"] == "hook_action" and e["data"].get("action") == "micro_compact"
    ]
    assert micro, "no micro_compact hook_action in loop events"
    # The fold reached the wire: a later request carries the marker.
    assert any(
        "[tool output folded to save context]"
        in json.dumps(req.get("messages") or [])
        for req in mock.requests[1:]
    )


# ---------------------------------------------------------------------------
# R16 P1: on_stream_chunk — stream.chunk deltas and the raw channel
# ---------------------------------------------------------------------------


async def test_stream_chunk_notifications_carry_deltas_and_raw_chunks(
    host_client_factory: Any, mock_openai: Any
) -> None:
    def responder(_body: dict[str, Any], _index: int) -> list[dict[str, Any]]:
        return [
            {"choices": [{"index": 0, "delta": {"content": "CHUNK_A"}}]},
            {"choices": [{"index": 0, "delta": {"content": "CHUNK_B"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]

    mock = mock_openai(responder)
    client = await host_client_factory()
    result = await client.request(
        "agent.chat.stream", _stream_params(mock, streamRawChunks=True)
    )
    stream_id = result["streamId"]
    await _await_done(client, stream_id)

    chunks = [
        p.get("params") or {}
        for p in client.notifications
        if p.get("method") == "stream.chunk"
        and (p.get("params") or {}).get("streamId") == stream_id
    ]
    deltas = [c["delta"] for c in chunks if "delta" in c]
    assert "CHUNK_A" in deltas and "CHUNK_B" in deltas
    # streamRawChunks: the pre-digestion raw chunk rides the same
    # notification under rawChunk.
    assert any("rawChunk" in c for c in chunks)


# ---------------------------------------------------------------------------
# R16 P2: Brave web-search provider against a loopback Brave endpoint
# ---------------------------------------------------------------------------


class _BraveMock:
    """Loopback ``GET /res/v1/web/search`` speaking the Brave response shape."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if not self.path.startswith("/res/v1/web/search"):
                    self.send_error(404)
                    return
                outer.requests.append(
                    {
                        "path": self.path,
                        "token": self.headers.get("X-Subscription-Token"),
                    }
                )
                payload = {
                    "web": {
                        "results": [
                            {
                                "url": "https://example.test/steerable",
                                "title": "BRAVE_E2E_HIT",
                                "description": "loopback brave result",
                                "age": "2026-09-01",
                            }
                        ]
                    }
                }
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


async def test_brave_search_provider_end_to_end(
    host_client_factory: Any, mock_openai: Any
) -> None:
    brave = _BraveMock()
    try:
        def responder(_body: dict[str, Any], index: int) -> list[dict[str, Any]]:
            if index == 0:
                return sse_tool_call(
                    "web_search", {"query": "steerable framework"}, call_id="call_search"
                )
            return sse_text("SEARCH_E2E_OK")

        mock = mock_openai(responder)
        client = await host_client_factory(
            {
                "STEERABLE_WEB_SEARCH_PROVIDER": "brave",
                "STEERABLE_WEB_SEARCH_API_KEY": "brave-e2e-key",
                "STEERABLE_WEB_SEARCH_BASE_URL": brave.base_url,
            }
        )
        # The chat path's tools array is host-advertised (the desktop owns
        # the model's tool face); the sidecar registry owns dispatch. Pass
        # the descriptor the way a host would.
        web_search = {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
        result = await client.request(
            "agent.chat.stream", _stream_params(mock, tools=[web_search])
        )
        await _await_done(client, result["streamId"])

        # The key registered the tool and the model was offered it…
        offered = {
            (t.get("function") or {}).get("name")
            for t in mock.requests[0].get("tools") or []
        }
        assert "web_search" in offered
        # …the Brave endpoint saw the query with the subscription token…
        assert brave.requests, "Brave loopback never contacted"
        assert brave.requests[0]["token"] == "brave-e2e-key"
        assert "q=steerable" in brave.requests[0]["path"].replace("+", "").replace(
            "%20", ""
        )
        # …and the hit came back to the model as the tool result.
        tool_msgs = [
            m for m in mock.requests[1]["messages"] if m.get("role") == "tool"
        ]
        assert any("BRAVE_E2E_HIT" in str(m.get("content")) for m in tool_msgs)
    finally:
        brave.close()


# ---------------------------------------------------------------------------
# R16 P4: session tree — fork fan-out, cousin resolution
# ---------------------------------------------------------------------------


async def test_session_tree_resolves_cousin_branches(
    host_client_factory: Any, mock_openai: Any
) -> None:
    mock = mock_openai(lambda _body, _index: sse_text("ROOT_TURN"))
    client = await host_client_factory()
    result = await client.request(
        "agent.chat.stream", _stream_params(mock, chatId="chat-a")
    )
    await _await_done(client, result["streamId"])

    # A → B, A → C, B → D (C and D are cousins).
    fork_b = await client.request(
        "agent.session.fork",
        {"recordId": "chat-a", "newRecordId": "chat-b", "label": "branch-b"},
    )
    assert fork_b["recordId"] == "chat-b"
    await client.request(
        "agent.session.fork",
        {"recordId": "chat-a", "newRecordId": "chat-c", "label": "branch-c"},
    )
    await client.request(
        "agent.session.fork",
        {"recordId": "chat-b", "newRecordId": "chat-d", "label": "branch-d"},
    )

    tree = await client.request("agent.session.tree", {"recordId": "chat-c"})
    assert tree["recordId"] == "chat-c"
    assert tree["nodeCount"] == 4
    assert tree["truncated"] is False
    root = tree["tree"]
    assert root["recordId"] == "chat-a"
    children = {c["recordId"]: c for c in root["children"]}
    assert set(children) == {"chat-b", "chat-c"}
    assert [c["recordId"] for c in children["chat-b"]["children"]] == ["chat-d"]
    assert children["chat-c"]["depth"] == 1


# ---------------------------------------------------------------------------
# R16 P4: provider presets on the wire (auto / off / pinned) + describe RPCs
# ---------------------------------------------------------------------------


async def test_provider_presets_auto_off_and_pinned_on_the_wire(
    host_client_factory: Any, mock_openai: Any
) -> None:
    mock = mock_openai(lambda _body, _index: sse_text("ok"))
    client = await host_client_factory()

    async def one_turn(**extra: Any) -> dict[str, Any]:
        before = len(mock.requests)
        result = await client.request(
            "agent.chat.stream",
            _stream_params(mock, model="deepseek-chat", **extra),
        )
        await _await_done(client, result["streamId"])
        assert len(mock.requests) == before + 1
        return mock.requests[-1]

    # Auto: the model-leaf entry matches across gateways (the loopback host
    # proves the host key is not what matched).
    body = await one_turn()
    assert body["temperature"] == 0.0
    # Off: the layer sends nothing.
    body = await one_turn(presets={"enabled": False})
    assert "temperature" not in body
    # Pinned override: the explicit preset applies regardless of the registry.
    body = await one_turn(
        presets={"override": {"temperature": 0.42, "extraBody": {"top_k": 7}}}
    )
    assert body["temperature"] == 0.42
    assert body["top_k"] == 7

    described = await client.request("presets.describe")
    assert any(
        row.get("modelPrefix") == "deepseek" for row in described["presets"]
    )
    resolved = await client.request(
        "presets.resolve",
        {"baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    )
    assert resolved["preset"] == {"temperature": 0.0}


# ---------------------------------------------------------------------------
# R16 P2: single config system — config.get serves the layered merge
# ---------------------------------------------------------------------------


async def test_config_get_reports_layered_merge(
    host_client_factory: Any,
) -> None:
    client = await host_client_factory()
    base = await client.request("config.get")
    assert base["version"] and base["protocolVersion"]
    merged = await client.request("config.get", {"merged": True})
    assert isinstance(merged["merged"], dict) and merged["merged"]


# ---------------------------------------------------------------------------
# R16 P4: plugin runtime lifecycle in a fresh interpreter process
# ---------------------------------------------------------------------------

_PLUGIN_V1 = "def register(router):\n    router.register(lambda: 'v1', name='greet')\n"
_PLUGIN_V2 = "def register(router):\n    router.register(lambda: 'v2', name='greet')\n"

#: Driver run as its own interpreter: the lifecycle (including the
#: exec_module hot reload) is exercised against real import machinery, not
#: the test process's module cache.
_PLUGIN_DRIVER = """
import json
import sys
from pathlib import Path

from steerable_agent_runtime import ToolRouter
from steerable_agent_runtime.plugins import DirectorySource, PluginRegistry

plugin_dir = Path(sys.argv[1])
router = ToolRouter()
registry = PluginRegistry(router)
out = {}

out["loaded"] = registry.load_source(DirectorySource(plugin_dir))
out["v1"] = router.get("greet").handler()

registry.disable("greeter")
out["disabled"] = router.get("greet") is None

registry.enable("greeter")
out["enabled"] = router.get("greet") is not None

plugin_dir.joinpath("greeter.py").write_text(sys.argv[2])
registry.reload("greeter")
out["v2"] = router.get("greet").handler()

registry.unload("greeter")
out["unloaded"] = router.get("greet") is None and registry.get("greeter") is None

print("PLUGIN_E2E_RESULT:" + json.dumps(out))
"""


async def test_plugin_runtime_lifecycle_in_a_real_process(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "greeter.py").write_text(_PLUGIN_V1, encoding="utf-8")
    driver = tmp_path / "plugin_driver.py"
    driver.write_text(_PLUGIN_DRIVER, encoding="utf-8")

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(driver),
        str(plugin_dir),
        _PLUGIN_V2,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_env(tmp_path),
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
    assert proc.returncode == 0, stderr.decode()[-2000:]
    marker = next(
        line
        for line in stdout.decode().splitlines()
        if line.startswith("PLUGIN_E2E_RESULT:")
    )
    out = json.loads(marker.removeprefix("PLUGIN_E2E_RESULT:"))
    assert out == {
        "loaded": ["greeter"],
        "v1": "v1",
        "disabled": True,
        "enabled": True,
        "v2": "v2",
        "unloaded": True,
    }


async def test_skill_tool_loads_body_over_real_sidecar(
    host_client_factory: Any, mock_openai: Any, tmp_path: Path
) -> None:
    """The ``skill`` tool on the default (layered) mode: the catalog reaches
    the model, a call crosses stdio, and the full SKILL.md body comes back
    as the tool result."""
    root = tmp_path / "skills"
    skill_dir = root / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: demo\n"
        "description: Demo skill for the e2e pass.\n"
        "---\n"
        "\n"
        "# Demo\n"
        "DEMO_SKILL_BODY_MARKER\n",
        encoding="utf-8",
    )

    def responder(body: dict[str, Any], index: int) -> list[dict[str, Any]]:
        if index == 0:
            return sse_tool_call("skill", {"name": "demo"}, call_id="c_skill")
        return sse_text("SKILL_E2E_OK")

    mock = mock_openai(responder)
    client = await host_client_factory()
    result = await client.request(
        "agent.chat.stream", _stream_params(mock, skills={"roots": [str(root)]})
    )
    await _await_done(client, result["streamId"])

    # The catalog advertised the tool and the model's call dispatched.
    assert "skill" in model_tool_names(mock.requests[0].get("tools"))
    assert len(mock.requests) == 2
    tool_msgs = [
        m for m in mock.requests[1]["messages"] if m.get("role") == "tool"
    ]
    assert any(
        "DEMO_SKILL_BODY_MARKER" in str(m.get("content")) for m in tool_msgs
    )
