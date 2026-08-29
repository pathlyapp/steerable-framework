"""Wave 3: approval algebra — 8-variant decisions, three persistence scopes.

Mirrors codex's ``ReviewDecision`` algebra: allow/deny across request /
session / durable scopes, ``Denied{reason}`` fed back to the model as a tool
result while the run continues, ``Abort`` ending the turn, and a fail-closed
timeout. Enforcement lives in ``ApprovalExecutor``, a ToolExecutor decorator,
so it works in front of any dispatch path (router, host reverse channel, MCP).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall

from steerable_agent_runtime import (
    ApprovalAborted,
    ApprovalDecision,
    ApprovalExecutor,
    ApprovalRequest,
    AutoApprover,
    CoreLoop,
    InMemoryApprovalStore,
    JsonApprovalStore,
    LLMMessage,
    LoopContext,
    RouterToolExecutor,
    SessionApprovalCache,
    ToolRouter,
    tool,
)
from steerable_agent_runtime.llm import LLMStreamChunk, LLMUsage
from steerable_agent_runtime.storage import InMemoryStorage


def _msg(role: str, text: str) -> LLMMessage:
    return LLMMessage.text_of(role, text)  # type: ignore[arg-type]


def _provider(script: list[dict[str, Any]]):
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
                if entry.get("content"):
                    yield LLMStreamChunk(content_delta=entry["content"])
                for call in entry.get("tool_calls", []):
                    yield LLMStreamChunk(tool_call_delta=call)
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop",
                    usage=LLMUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
                )

            return _gen()

    return _FakeProvider()


async def _collect(loop_run: AsyncIterator) -> list[Any]:
    return [e async for e in loop_run]


class _ScriptedApprover:
    """Approver returning queued decisions; records every request."""

    def __init__(self, decisions: list[ApprovalDecision]) -> None:
        self._decisions = list(decisions)
        self.requests: list[ApprovalRequest] = []

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        if self._decisions:
            return self._decisions.pop(0)
        return ApprovalDecision("deny_once", "script exhausted")


def _router_with_tools() -> tuple[ToolRouter, dict[str, int]]:
    router = ToolRouter()
    calls: dict[str, int] = {"read_file": 0, "delete_file": 0}

    @tool(router=router, description="Read a file", mode="read")
    async def read_file(path: str) -> dict[str, str]:
        calls["read_file"] += 1
        return {"content": f"contents of {path}"}

    @tool(router=router, description="Delete a file", mode="destructive")
    async def delete_file(path: str) -> dict[str, str]:
        calls["delete_file"] += 1
        return {"deleted": path}

    return router, calls


def _call(name: str, **arguments: Any) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=arguments)


# ---------------------------------------------------------------------------
# Decision algebra
# ---------------------------------------------------------------------------


def test_decision_allowed_property() -> None:
    for kind in ("allow_once", "allow_for_session", "allow_always"):
        assert ApprovalDecision(kind).allowed, kind  # type: ignore[arg-type]
    for kind in ("deny_once", "deny_for_session", "deny_always", "abort", "timed_out"):
        assert not ApprovalDecision(kind).allowed, kind  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AutoApprover (headless: never blocks, fails closed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_approver_allows_safe_modes() -> None:
    approver = AutoApprover()
    for mode in ("read", "safe_write"):
        decision = await approver.approve(
            ApprovalRequest(tool_name="t", arguments={}, mode=mode, category="t")  # type: ignore[arg-type]
        )
        assert decision.kind == "allow_once"


@pytest.mark.asyncio
async def test_auto_approver_denies_destructive_and_other() -> None:
    approver = AutoApprover()
    for mode in ("destructive", "other"):
        decision = await approver.approve(
            ApprovalRequest(tool_name="t", arguments={}, mode=mode, category="t")  # type: ignore[arg-type]
        )
        assert decision.kind == "deny_once"
        assert "headless" in decision.reason


# ---------------------------------------------------------------------------
# ApprovalExecutor over a router
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allow_once_executes_and_bridges_require_consent() -> None:
    router, calls = _router_with_tools()
    # delete_file is destructive → require_consent=True on registration; the
    # approval bridge (ctx.consent_granted) must satisfy the router's gate.
    executor = ApprovalExecutor(
        RouterToolExecutor(router),
        _ScriptedApprover([ApprovalDecision("allow_once")]),
    )
    result = await executor.execute(_call("delete_file", path="/tmp/x"), LoopContext())
    assert result.success, result.error
    assert calls["delete_file"] == 1


@pytest.mark.asyncio
async def test_deny_once_returns_denied_result_without_executing() -> None:
    router, calls = _router_with_tools()
    executor = ApprovalExecutor(
        RouterToolExecutor(router),
        _ScriptedApprover([ApprovalDecision("deny_once", "not today")]),
    )
    result = await executor.execute(_call("delete_file", path="/tmp/x"), LoopContext())
    assert not result.success
    assert result.error == "approval_denied"
    assert result.data["approval"] == "deny_once"
    assert "not today" in result.data["message"]
    assert calls["delete_file"] == 0


@pytest.mark.asyncio
async def test_abort_raises() -> None:
    router, _calls = _router_with_tools()
    executor = ApprovalExecutor(
        RouterToolExecutor(router),
        _ScriptedApprover([ApprovalDecision("abort", "stop everything")]),
    )
    with pytest.raises(ApprovalAborted, match="stop everything"):
        await executor.execute(_call("delete_file", path="/tmp/x"), LoopContext())


@pytest.mark.asyncio
async def test_session_scope_caches_per_category() -> None:
    router, calls = _router_with_tools()
    approver = _ScriptedApprover([ApprovalDecision("allow_for_session")])
    executor = ApprovalExecutor(
        RouterToolExecutor(router), approver, session=SessionApprovalCache()
    )
    ctx = LoopContext()
    first = await executor.execute(_call("delete_file", path="/a"), ctx)
    second = await executor.execute(_call("delete_file", path="/b"), ctx)
    assert first.success and second.success
    assert calls["delete_file"] == 2
    # The second call in the same category was decided by the cache.
    assert len(approver.requests) == 1


@pytest.mark.asyncio
async def test_session_deny_caches_per_category() -> None:
    router, calls = _router_with_tools()
    approver = _ScriptedApprover([ApprovalDecision("deny_for_session", "no deletes")])
    executor = ApprovalExecutor(
        RouterToolExecutor(router), approver, session=SessionApprovalCache()
    )
    ctx = LoopContext()
    first = await executor.execute(_call("delete_file", path="/a"), ctx)
    second = await executor.execute(_call("delete_file", path="/b"), ctx)
    assert first.error == "approval_denied" and second.error == "approval_denied"
    assert second.data["approval"] == "deny_for_session"
    assert calls["delete_file"] == 0
    assert len(approver.requests) == 1


@pytest.mark.asyncio
async def test_durable_scope_persists_across_executors() -> None:
    router, calls = _router_with_tools()
    store = InMemoryApprovalStore()
    approver = _ScriptedApprover([ApprovalDecision("allow_always")])
    executor = ApprovalExecutor(
        RouterToolExecutor(router), approver, store=store
    )
    result = await executor.execute(_call("delete_file", path="/a"), LoopContext())
    assert result.success

    # A new executor over the same store (a later session) applies the
    # durable decision without asking any approver.
    store2_view = ApprovalExecutor(
        RouterToolExecutor(router),
        _ScriptedApprover([]),  # would deny if asked
        store=store,
    )
    again = await store2_view.execute(_call("delete_file", path="/b"), LoopContext())
    assert again.success, again.error
    assert calls["delete_file"] == 2


@pytest.mark.asyncio
async def test_durable_deny_persists() -> None:
    router, calls = _router_with_tools()
    store = InMemoryApprovalStore()
    executor = ApprovalExecutor(
        RouterToolExecutor(router),
        _ScriptedApprover([ApprovalDecision("deny_always", "never delete")]),
        store=store,
    )
    await executor.execute(_call("delete_file", path="/a"), LoopContext())

    later = ApprovalExecutor(
        RouterToolExecutor(router), _ScriptedApprover([]), store=store
    )
    result = await later.execute(_call("delete_file", path="/b"), LoopContext())
    assert result.error == "approval_denied"
    assert result.data["approval"] == "deny_always"
    assert calls["delete_file"] == 0


@pytest.mark.asyncio
async def test_durable_wins_over_session() -> None:
    router, calls = _router_with_tools()
    store = InMemoryApprovalStore()
    store.put("delete_file", "deny_always")
    session = SessionApprovalCache()
    session.put("delete_file", "allow_for_session")
    executor = ApprovalExecutor(
        RouterToolExecutor(router),
        _ScriptedApprover([]),
        session=session,
        store=store,
    )
    result = await executor.execute(_call("delete_file", path="/a"), LoopContext())
    assert result.error == "approval_denied"
    assert calls["delete_file"] == 0


@pytest.mark.asyncio
async def test_approver_timeout_fails_closed() -> None:
    class _HangingApprover:
        async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
            await asyncio.sleep(60)
            return ApprovalDecision("allow_once")  # pragma: no cover

    router, calls = _router_with_tools()
    executor = ApprovalExecutor(
        RouterToolExecutor(router), _HangingApprover(), timeout_s=0.05
    )
    result = await executor.execute(_call("delete_file", path="/a"), LoopContext())
    assert result.error == "approval_denied"
    assert result.data["approval"] == "timed_out"
    assert calls["delete_file"] == 0


@pytest.mark.asyncio
async def test_custom_resolver_groups_categories() -> None:
    """Two tools sharing a resolver-assigned category share one decision."""
    router, calls = _router_with_tools()
    approver = _ScriptedApprover([ApprovalDecision("allow_for_session")])

    def by_mode(call: ToolCall, ctx: LoopContext) -> ApprovalRequest:
        registered = router.get(call.name)
        return ApprovalRequest(
            tool_name=call.name,
            arguments=call.arguments or {},
            mode=registered.mode if registered else "other",
            category=registered.mode if registered else "other",
            round_index=ctx.round_index,
        )

    executor = ApprovalExecutor(
        RouterToolExecutor(router),
        approver,
        session=SessionApprovalCache(),
        resolve=by_mode,
    )
    ctx = LoopContext()
    await executor.execute(_call("delete_file", path="/a"), ctx)
    # A second destructive tool would share the "destructive" category; with
    # only one destructive tool registered, re-call the same one.
    await executor.execute(_call("delete_file", path="/b"), ctx)
    assert calls["delete_file"] == 2
    assert len(approver.requests) == 1


@pytest.mark.asyncio
async def test_durable_variant_without_store_degrades_to_session() -> None:
    router, calls = _router_with_tools()
    approver = _ScriptedApprover([ApprovalDecision("allow_always")])
    executor = ApprovalExecutor(
        RouterToolExecutor(router), approver, session=SessionApprovalCache()
    )
    ctx = LoopContext()
    await executor.execute(_call("delete_file", path="/a"), ctx)
    await executor.execute(_call("delete_file", path="/b"), ctx)
    assert calls["delete_file"] == 2
    assert len(approver.requests) == 1  # degraded to session scope


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------


def test_session_cache_rejects_non_session_kinds() -> None:
    cache = SessionApprovalCache()
    with pytest.raises(ValueError, match="not a session-scope kind"):
        cache.put("bash", "allow_always")


def test_json_store_round_trip(tmp_path) -> None:
    path = tmp_path / "approvals" / "decisions.json"
    store = JsonApprovalStore(path)
    assert store.load() == {}
    store.put("bash", "allow_always")
    store.put("delete_file", "deny_always")
    assert JsonApprovalStore(path).load() == {
        "bash": "allow_always",
        "delete_file": "deny_always",
    }


def test_json_store_rejects_non_durable_kinds(tmp_path) -> None:
    store = JsonApprovalStore(tmp_path / "d.json")
    with pytest.raises(ValueError, match="not a durable-scope kind"):
        store.put("bash", "allow_once")


def test_json_store_corrupt_file_fails_closed(tmp_path) -> None:
    path = tmp_path / "d.json"
    path.write_text("{not json", encoding="utf-8")
    assert JsonApprovalStore(path).load() == {}


# ---------------------------------------------------------------------------
# Loop integration: denial feeds back, abort ends the turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_denial_feeds_back_and_continues() -> None:
    router, calls = _router_with_tools()
    executor = ApprovalExecutor(
        RouterToolExecutor(router),
        _ScriptedApprover([ApprovalDecision("deny_once", "user said no")]),
    )
    provider = _provider(
        [
            {"tool_calls": [ToolCall(id="d1", name="delete_file", arguments={"path": "/x"})]},
            {"content": "Understood, I will not delete anything."},
        ]
    )
    loop = CoreLoop(provider, executor)
    events = await _collect(
        loop.run([_msg("user", "delete /x")], tools=router.describe_model())
    )

    assert events[-1].kind == "completion"
    assert calls["delete_file"] == 0
    # The denial reached the model as a tool result; the model reacted and
    # completed instead of the turn failing.
    tool_messages = [
        m
        for call in provider.calls
        for m in call
        if m.role == "tool" and m.name == "delete_file"
    ]
    assert tool_messages, "denial must be model-visible"
    assert "user said no" in tool_messages[0].content_text
    assert events[-1].data["status"] == "completed"


@pytest.mark.asyncio
async def test_loop_abort_ends_turn_with_full_tool_responses() -> None:
    router, calls = _router_with_tools()
    executor = ApprovalExecutor(
        RouterToolExecutor(router),
        _ScriptedApprover([ApprovalDecision("abort", "user cancelled")]),
    )
    # Two serial calls (read_file is not concurrency-safe by default): the
    # first aborts, the second must still gain a (skip) tool response or the
    # transcript would dangle.
    provider = _provider(
        [
            {
                "tool_calls": [
                    ToolCall(id="d1", name="delete_file", arguments={"path": "/x"}),
                    ToolCall(id="d2", name="delete_file", arguments={"path": "/y"}),
                ]
            },
            {"content": "unreachable"},
        ]
    )
    storage = InMemoryStorage()
    loop = CoreLoop(provider, executor, history_store=storage, record_id="chat_1")
    events = await _collect(
        loop.run(
            [_msg("user", "delete both")],
            tools=router.describe_model(),
            chat_id="chat_1",
        )
    )

    assert calls["delete_file"] == 0
    assert events[-1].data["status"] == "failed"
    assert "approval aborted" in events[-1].data["reason"]
    assert "user cancelled" in events[-1].data["reason"]

    # Every tool_call in the record has a response: the aborted call's error
    # result and a skip notice for the never-executed second call.
    entries = await storage.list_history("chat_1")
    kinds = [e["kind"] for e in entries]
    assert kinds == ["user", "assistant", "tool", "loop.abort_skip"]
    aborted = entries[2]["message"]["content"][0]["text"]
    skipped = entries[3]["message"]["content"][0]["text"]
    assert "user cancelled" in aborted
    assert "not executed" in skipped
    assert entries[3]["message"]["tool_call_id"] == "d2"
