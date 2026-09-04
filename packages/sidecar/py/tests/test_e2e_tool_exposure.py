"""E2E: tier-derived tool exposure through a real sidecar process.

Layer choice: the behavior under test spans ``ToolRouter`` exposure tiers,
``AssembledHarness.wire_tools`` / ``select_tools``, and the CoreLoop's tool
dispatch — all inside the sidecar process. The desktop chat path cannot
host a router-backed selection (host tools dispatch over the reverse
channel), so the honest real-process layer is a spawned helper sidecar
(``e2e_progressive_loop_sidecar.py``) that assembles the production
harness path — ``load_harness_spec`` → ``assemble_harness`` →
``wire_tools`` → ``select_tools`` → ``CoreLoop.run`` — over a real stdio
JSON-RPC transport, with the LLM replaced by the loopback mock. The unwired
case boots the *unmodified* ``Sidecar.serve()`` chat path with a
progressive bundled spec (``e2e_progressive_chat_sidecar.py``) so the
failure is observed on the wire.

What is proven through the real process:

- progressive selection offers exactly the direct tier plus ``tool_search``
  — deferred and hidden tools never reach the model's request;
- a ``tool_search`` call dispatches (no "unknown tool") and returns
  BM25-ranked deferred matches with the deferred count;
- a deferred-tier tool absent from the offered list dispatches by name once
  discovered;
- the hidden tier is neither offered nor searchable;
- an unwired progressive selection on the chat path fails the turn loud
  (``stream.error`` naming the remedy) before any provider request, instead
  of advertising an undispatchable tool.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from e2e_harness import (
    TESTS_DIR,
    sse_text,
    sse_tool_call,
)

_PROGRESSIVE_SPEC = {
    "context": ["null"],
    "retry": ["none"],
    "validator": "null",
    "tools": "progressive",
    "memory": "stateless",
    "orchestration": "single",
}

_BASH_DESCRIPTOR: dict[str, Any] = {
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


def _tool_names(body: dict[str, Any]) -> list[str]:
    return [(t.get("function") or {}).get("name") for t in body.get("tools") or []]


def _tool_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in body.get("messages") or [] if m.get("role") == "tool"]


async def test_progressive_turn_discovers_and_dispatches_deferred_tool(
    e2e_gate: None, sidecar_factory: Any, mock_openai: Any, tmp_path: Path
) -> None:
    spec_path = tmp_path / "progressive.harness.json"
    spec_path.write_text(json.dumps(_PROGRESSIVE_SPEC), encoding="utf-8")

    def responder(body: dict[str, Any], index: int) -> list[dict[str, Any]]:
        if index == 0:
            return sse_tool_call(
                "tool_search", {"query": "github"}, call_id="call_search"
            )
        if index == 1:
            return sse_tool_call(
                "mcp__github__create_issue",
                {"title": "found via search"},
                call_id="call_create",
            )
        return sse_text("E2E_PROGRESSIVE_OK")

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
            "instruction": "Find the GitHub tools and create an issue.",
        },
        timeout=60.0,
    )

    # (a) Selection offered the direct tier + tool_search, nothing else…
    assert result["offeredTools"] == ["echo", "tool_search"]
    # …and that is exactly what reached the model on the wire.
    assert len(mock.requests) == 3
    assert _tool_names(mock.requests[0]) == ["echo", "tool_search"]

    # (a-cont.) tool_search dispatched for real and BM25-ranked the deferred
    # tier: both GitHub tools match, the Linear tool scores zero, the hidden
    # tool is never searchable.
    search_messages = [
        m for m in _tool_messages(mock.requests[1]) if m.get("name") == "tool_search"
    ]
    assert len(search_messages) == 1
    search_payload = json.loads(search_messages[0]["content"])
    assert search_payload["success"] is True
    matches = search_payload["data"]["value"]["matches"]
    assert {m["name"] for m in matches} == {
        "mcp__github__create_issue",
        "mcp__github__list_prs",
    }
    assert search_payload["data"]["value"]["deferredCount"] == 3
    # Matches carry the full schema so the model can call immediately.
    assert all("parameters" in m for m in matches)

    # (b) The discovered deferred tool — never in the offered list —
    # dispatched by name and its result went back to the model.
    create_messages = [
        m
        for m in _tool_messages(mock.requests[2])
        if m.get("name") == "mcp__github__create_issue"
    ]
    assert len(create_messages) == 1
    create_payload = json.loads(create_messages[0]["content"])
    assert create_payload["success"] is True
    assert create_payload["data"]["value"]["issue"] == "found via search"

    # The turn completed with the scripted final answer.
    assert "E2E_PROGRESSIVE_OK" in result["content"]
    completions = [e for e in result["events"] if e["kind"] == "completion"]
    assert completions[-1]["data"]["status"] == "completed"


async def test_unwired_progressive_selection_fails_loud_on_chat_path(
    e2e_gate: None, sidecar_factory: Any, mock_openai: Any
) -> None:
    """The chat path has no router to bind; selection must raise, and the
    turn must die before any provider request — never offer a tool_search
    descriptor that nothing can dispatch."""
    mock = mock_openai(lambda _body, _index: sse_text("unreachable"))
    client = await sidecar_factory(
        [sys.executable, str(TESTS_DIR / "e2e_progressive_chat_sidecar.py")],
    )

    result = await client.request(
        "agent.chat.stream",
        {
            "provider": "openai_compat",
            "model": "mock-e2e",
            "baseUrl": mock.base_url,
            "apiKey": "e2e-not-a-real-key",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [_BASH_DESCRIPTOR],
            "useCoreLoop": True,
        },
    )
    stream_id = result["streamId"]

    error = await client.wait_for_notification(
        "stream.error",
        predicate=lambda p: p.get("streamId") == stream_id,
        timeout=30.0,
    )
    assert "wire_tools" in error["message"]
    assert "progressive" in error["message"]

    # The failure happened before the provider was ever called, and no
    # terminal done follows the error (the ping round-trip proves the
    # sidecar is still healthy and has flushed everything it will send).
    await client.request("system.ping")
    assert mock.requests == []
    assert not any(
        p.get("method") == "stream.done"
        and (p.get("params") or {}).get("streamId") == stream_id
        for p in client.notifications
    )
