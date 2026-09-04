"""Test-only sidecar: a wired progressive-disclosure CoreLoop over stdio.

Run as a subprocess by ``test_e2e_tool_exposure.py``. Registers a small
tiered tool inventory (one direct tool, three deferred MCP-style tools, one
hidden tool) on a real ``ToolRouter``, assembles the harness from the
caller-supplied spec file with the production ``load_harness_spec`` /
``assemble_harness`` / ``wire_tools`` / ``select_tools`` path, and runs one
CoreLoop turn against the caller-supplied provider endpoint (the test's
loopback mock).

One RPC method:

- ``test.run_turn`` ``{specPath, provider, instruction, maxRounds?}`` →
  ``{offeredTools, events, content}`` — the descriptor names the selection
  offered the model, the sanitized loop events, and the final content.

Diagnostics go to stderr only; stdout carries NDJSON frames exclusively.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from steerable_agent_runtime import (
    CoreLoop,
    LoopConfig,
    RouterToolExecutor,
    ToolRouter,
)
from steerable_agent_runtime.harness_spec import assemble_harness, load_harness_spec
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.transport.stdio_jsonrpc import JsonRpcServer
from steerable_sidecar.sidecar import default_llm_provider_factory


def _build_router() -> ToolRouter:
    router = ToolRouter()

    def echo(text: str = "") -> dict[str, Any]:
        """Echo text back (the direct-tier tool)."""
        return {"echo": text}

    def mcp__github__create_issue(title: str = "", body: str = "") -> dict[str, Any]:
        """Create a GitHub issue in a repository."""
        return {"issue": title, "url": "https://example.test/issues/1"}

    def mcp__github__list_prs(state: str = "open") -> dict[str, Any]:
        """List GitHub pull requests for a repository."""
        return {"prs": [], "state": state}

    def mcp__linear__create_issue(title: str = "") -> dict[str, Any]:
        """Create a Linear issue in a project."""
        return {"linear_issue": title}

    def internal_reset() -> dict[str, Any]:
        """Reset internal state (operator-only; never model-visible)."""
        return {"reset": True}

    router.register(echo, name="echo", mode="read", exposure="direct")
    router.register(
        mcp__github__create_issue,
        name="mcp__github__create_issue",
        mode="write",
        exposure="deferred",
    )
    router.register(
        mcp__github__list_prs,
        name="mcp__github__list_prs",
        mode="read",
        exposure="deferred",
    )
    router.register(
        mcp__linear__create_issue,
        name="mcp__linear__create_issue",
        mode="write",
        exposure="deferred",
    )
    router.register(
        internal_reset, name="internal_reset", mode="destructive", exposure="hidden"
    )
    return router


def _sanitize(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


async def _run_turn(params: dict[str, Any] | None) -> dict[str, Any]:
    params = params or {}
    spec = load_harness_spec(params["specPath"])
    router = _build_router()
    harness = assemble_harness(spec)
    harness.wire_tools(router)
    offered = harness.select_tools(router.describe())

    provider = default_llm_provider_factory(params["provider"])
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(max_rounds=int(params.get("maxRounds") or 8)),
        hooks=harness.hooks,
    )
    events: list[dict[str, Any]] = []
    content_parts: list[str] = []
    async for event in loop.run(
        [LLMMessage.text_of("user", params.get("instruction") or "begin")],
        tools=offered,
    ):
        events.append({"kind": event.kind, "data": _sanitize(event.data)})
        if event.kind == "content_delta":
            content_parts.append(str(event.data.get("delta") or ""))
    return {
        "offeredTools": [
            (d.get("function") or {}).get("name") for d in offered
        ],
        "events": events,
        "content": "".join(content_parts),
    }


async def _main() -> None:
    server = JsonRpcServer()
    server.register("test.run_turn", _run_turn)
    await server.serve_stdio()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    except Exception:  # boot failures must be visible on stderr, not silent
        import traceback

        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
