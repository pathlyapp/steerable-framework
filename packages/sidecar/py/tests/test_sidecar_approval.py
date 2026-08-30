"""Wave 3: sidecar approval wiring — ``approval`` param on chat.stream.

``{"mode": "auto"}`` is the headless policy (safe modes auto-approve, the
rest auto-deny — the run never hangs); ``{"mode": "host"}`` asks the host UI
over the reverse channel and fails closed when the host can't answer.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime.llm import LLMStreamChunk, LLMUsage

from steerable_sidecar.sidecar import Sidecar


class _ScriptedProvider:
    name = "scripted"
    model = "scripted-model"

    def __init__(self, script: list[list[LLMStreamChunk]]):
        self._script = script
        self._round = 0

    async def complete(self, *args, **kwargs):
        raise NotImplementedError

    def stream(self, messages, **kwargs):
        chunks = self._script[min(self._round, len(self._script) - 1)]
        self._round += 1

        async def _gen():
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


class _HostWriter:
    """Pretends to be the Electron host: answers reverse ``tool.invoke``
    requests from a scripted table and ``approval.request`` from a decision
    table (``None`` = no handler, never responds)."""

    def __init__(self, server, results: dict[str, dict], approvals: dict | None = None):
        self._server = server
        self._results = results
        self._approvals = approvals
        self.reverse_calls: list[dict] = []
        self.approval_requests: list[dict] = []

    def write(self, data: bytes):
        payload = json.loads(data)
        if not (isinstance(payload.get("id"), str) and payload.get("method")):
            return len(data)
        if payload["method"] == "tool.invoke":
            self.reverse_calls.append(payload["params"])
            if payload["params"]["name"] not in self._results:
                return len(data)
            result = self._results[payload["params"]["name"]]
        elif payload["method"] == "approval.request":
            self.approval_requests.append(payload["params"])
            if self._approvals is None:
                return len(data)  # no handler → never responds
            result = self._approvals
        else:
            return len(data)

        async def _respond() -> None:
            await self._server.handle_frame(
                json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result})
            )

        asyncio.ensure_future(_respond())
        return len(data)

    async def drain(self) -> None:
        return None


def _frame(method: str, params: dict | None = None) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


async def _run_stream(sidecar: Sidecar, params: dict) -> None:
    response = await sidecar.server.handle_frame(_frame("agent.chat.stream", params))
    assert "error" not in response, response
    task = sidecar._streams.get(response["result"]["streamId"])
    if task is not None:
        await task


def _base_params(**extra) -> dict:
    return {
        "provider": "openai_compat",
        "model": "fake",
        "messages": [{"role": "user", "content": "go"}],
        "useCoreLoop": True,
        "toolsViaHost": True,
        **extra,
    }


@pytest.mark.asyncio
async def test_auto_mode_denies_unsafe_tool_without_host_roundtrip() -> None:
    provider = _ScriptedProvider(
        [
            # local_exec_shell classifies as "other" → AutoApprover denies.
            _tool_round(ToolCall(id="c1", name="local_exec_shell", arguments={"command": "ls"})),
            _text_round("cannot run that"),
        ]
    )
    sidecar = Sidecar(llm_provider_factory=lambda _params: provider)
    sidecar._transport = _CapturingTransport()  # type: ignore[attr-defined]
    host = _HostWriter(sidecar.server, {"local_exec_shell": {"success": True}})
    sidecar.server.attach_writer(host)

    await _run_stream(
        sidecar, _base_params(approval={"mode": "auto"}, chatId="chat-auto")
    )

    # Denied before reaching the host: no tool.invoke reverse call happened.
    assert host.reverse_calls == []
    events = sidecar._transport.events  # type: ignore[attr-defined]
    results = [p for m, p in events if m == "stream.chunk" and "toolResult" in p]
    assert results[0]["toolResult"]["success"] is False
    assert results[0]["toolResult"]["error"] == "approval_denied"
    done = [p for m, p in events if m == "stream.done"]
    assert done[0]["status"] == "completed"  # denial feeds back, run continues


@pytest.mark.asyncio
async def test_auto_mode_allows_read_tool() -> None:
    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="c1", name="read_file", arguments={"path": "/a"})),
            _text_round("read it"),
        ]
    )
    sidecar = Sidecar(llm_provider_factory=lambda _params: provider)
    sidecar._transport = _CapturingTransport()  # type: ignore[attr-defined]
    host = _HostWriter(
        sidecar.server, {"read_file": {"success": True, "data": {"value": "x"}}}
    )
    sidecar.server.attach_writer(host)

    await _run_stream(sidecar, _base_params(approval={"mode": "auto"}))

    assert [c["name"] for c in host.reverse_calls] == ["read_file"]


@pytest.mark.asyncio
async def test_host_mode_asks_and_executes_on_allow() -> None:
    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="c1", name="delete_file", arguments={"path": "/x"})),
            _text_round("deleted"),
        ]
    )
    sidecar = Sidecar(llm_provider_factory=lambda _params: provider)
    sidecar._transport = _CapturingTransport()  # type: ignore[attr-defined]
    host = _HostWriter(
        sidecar.server,
        {"delete_file": {"success": True, "data": {"value": "gone"}}},
        approvals={"kind": "allow_once", "reason": "user clicked allow"},
    )
    sidecar.server.attach_writer(host)

    await _run_stream(sidecar, _base_params(approval={"mode": "host"}))

    assert len(host.approval_requests) == 1
    assert host.approval_requests[0]["toolName"] == "delete_file"
    assert host.approval_requests[0]["mode"] == "destructive"
    assert [c["name"] for c in host.reverse_calls] == ["delete_file"]


@pytest.mark.asyncio
async def test_host_mode_denial_skips_execution() -> None:
    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="c1", name="delete_file", arguments={"path": "/x"})),
            _text_round("ok, keeping it"),
        ]
    )
    sidecar = Sidecar(llm_provider_factory=lambda _params: provider)
    sidecar._transport = _CapturingTransport()  # type: ignore[attr-defined]
    host = _HostWriter(
        sidecar.server,
        {"delete_file": {"success": True}},
        approvals={"kind": "deny_once", "reason": "user said no"},
    )
    sidecar.server.attach_writer(host)

    await _run_stream(sidecar, _base_params(approval={"mode": "host"}))

    assert host.reverse_calls == []
    events = sidecar._transport.events  # type: ignore[attr-defined]
    results = [p for m, p in events if m == "stream.chunk" and "toolResult" in p]
    assert results[0]["toolResult"]["error"] == "approval_denied"


@pytest.mark.asyncio
async def test_host_mode_unreachable_host_fails_closed() -> None:
    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="c1", name="read_file", arguments={"path": "/a"})),
            _text_round("denied anyway"),
        ]
    )
    sidecar = Sidecar(llm_provider_factory=lambda _params: provider)
    sidecar._transport = _CapturingTransport()  # type: ignore[attr-defined]
    # No approval handler on the host → the request is never answered.
    host = _HostWriter(
        sidecar.server, {"read_file": {"success": True}}, approvals=None
    )
    sidecar.server.attach_writer(host)

    await _run_stream(
        sidecar,
        _base_params(approval={"mode": "host", "timeoutMs": 100}),
    )

    # Fail closed: even a read tool is denied when the host can't answer.
    assert host.reverse_calls == []
    events = sidecar._transport.events  # type: ignore[attr-defined]
    results = [p for m, p in events if m == "stream.chunk" and "toolResult" in p]
    assert results[0]["toolResult"]["error"] == "approval_denied"


@pytest.mark.asyncio
async def test_session_decision_carries_across_turns_of_one_chat() -> None:
    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="c1", name="delete_file", arguments={"path": "/a"})),
            _text_round("done one"),
            _tool_round(ToolCall(id="c2", name="delete_file", arguments={"path": "/b"})),
            _text_round("done two"),
        ]
    )
    sidecar = Sidecar(llm_provider_factory=lambda _params: provider)
    sidecar._transport = _CapturingTransport()  # type: ignore[attr-defined]
    host = _HostWriter(
        sidecar.server,
        {"delete_file": {"success": True, "data": {"value": "gone"}}},
        approvals={"kind": "allow_for_session"},
    )
    sidecar.server.attach_writer(host)

    params = _base_params(approval={"mode": "host"}, chatId="chat-session")
    await _run_stream(sidecar, params)
    await _run_stream(sidecar, params)

    # Both turns executed the tool, but the host was asked only once — the
    # second turn's decision came from the chat's session cache.
    assert [c["name"] for c in host.reverse_calls] == ["delete_file", "delete_file"]
    assert len(host.approval_requests) == 1


@pytest.mark.asyncio
async def test_no_approval_param_keeps_legacy_behavior() -> None:
    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="c1", name="delete_file", arguments={"path": "/x"})),
            _text_round("done"),
        ]
    )
    sidecar = Sidecar(llm_provider_factory=lambda _params: provider)
    sidecar._transport = _CapturingTransport()  # type: ignore[attr-defined]
    host = _HostWriter(sidecar.server, {"delete_file": {"success": True}})
    sidecar.server.attach_writer(host)

    await _run_stream(sidecar, _base_params())

    # No approval layer: the call goes straight to the host, no ask.
    assert [c["name"] for c in host.reverse_calls] == ["delete_file"]
    assert host.approval_requests == []


# ─── W2.4: policy rules + amendments ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_policy_rule_allows_without_host_roundtrip(tmp_path) -> None:
    """A matching allow rule decides; the host is never asked."""
    import json as _json

    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        _json.dumps(
            {
                "version": 1,
                "rules": [
                    {
                        "tool": "shell",
                        "decision": "allow",
                        "commandPrefix": ["echo"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="c1", name="shell", arguments={"command": "echo hi"})),
            _text_round("done"),
        ]
    )
    sidecar = Sidecar(llm_provider_factory=lambda _params: provider)
    sidecar._transport = _CapturingTransport()  # type: ignore[attr-defined]
    host = _HostWriter(
        sidecar.server,
        {"shell": {"success": True, "data": {"value": "hi"}}},
        approvals={"kind": "deny_once", "reason": "host would deny — must not be asked"},
    )
    sidecar.server.attach_writer(host)

    await _run_stream(
        sidecar,
        _base_params(approval={"mode": "host", "policyPath": str(policy_file)}),
    )

    assert host.approval_requests == []  # rule short-circuited the prompt
    assert [c["name"] for c in host.reverse_calls] == ["shell"]  # executed


@pytest.mark.asyncio
async def test_policy_rule_denies_without_host_roundtrip(tmp_path) -> None:
    import json as _json

    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        _json.dumps(
            {
                "version": 1,
                "rules": [{"tool": "shell", "decision": "deny", "commandPrefix": ["rm"]}],
            }
        ),
        encoding="utf-8",
    )
    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="c1", name="shell", arguments={"command": "rm -rf /x"})),
            _text_round("ok, not deleting"),
        ]
    )
    sidecar = Sidecar(llm_provider_factory=lambda _params: provider)
    sidecar._transport = _CapturingTransport()  # type: ignore[attr-defined]
    host = _HostWriter(
        sidecar.server,
        {"shell": {"success": True, "data": {"value": "deleted"}}},
        approvals={"kind": "allow_once"},
    )
    sidecar.server.attach_writer(host)

    await _run_stream(
        sidecar,
        _base_params(approval={"mode": "host", "policyPath": str(policy_file)}),
    )

    assert host.approval_requests == []
    assert host.reverse_calls == []  # never executed


@pytest.mark.asyncio
async def test_host_amendment_persists_and_applies_within_the_run(tmp_path) -> None:
    """The host approves with an amendment; the next matching call in the
    SAME run is not re-asked, and the rule lands on disk for future runs."""
    import json as _json

    policy_file = tmp_path / "policy.json"
    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="c1", name="shell", arguments={"command": "echo one"})),
            _tool_round(ToolCall(id="c2", name="shell", arguments={"command": "echo two"})),
            _text_round("done"),
        ]
    )
    sidecar = Sidecar(llm_provider_factory=lambda _params: provider)
    sidecar._transport = _CapturingTransport()  # type: ignore[attr-defined]
    host = _HostWriter(
        sidecar.server,
        {"shell": {"success": True, "data": {"value": "ok"}}},
        approvals={
            "kind": "allow_once",
            "amendment": {"decision": "allow", "commandPrefix": ["echo"]},
        },
    )
    sidecar.server.attach_writer(host)

    await _run_stream(
        sidecar,
        _base_params(approval={"mode": "host", "policyPath": str(policy_file)}),
    )

    # Asked once (first echo); the amendment covered the second.
    assert len(host.approval_requests) == 1
    assert [c["name"] for c in host.reverse_calls] == ["shell", "shell"]

    persisted = _json.loads(policy_file.read_text(encoding="utf-8"))
    assert persisted["rules"] == [
        {"tool": "shell", "decision": "allow", "commandPrefix": ["echo"]}
    ]
