"""W4-2: the tool_call_result LoopEvent lifts the sandbox marker.

The desktop tool card renders `data._sandbox` per call; the loop event is
what the sidecar's stream.chunk projection reads, so the marker must be
lifted out of ``result.data`` onto the event payload.
"""

from __future__ import annotations

import pytest
from steerable_agent_protocol.generated import ToolResult

from steerable_agent_runtime import (
    CoreLoop,
    RouterToolExecutor,
    SandboxedToolExecutor,
    ToolRouter,
)
from steerable_agent_runtime.llm import LLMMessage

from test_loop import collect, make_provider, tc


def _router_with_echo_shell() -> ToolRouter:
    router = ToolRouter()

    async def local_exec_shell(command: str) -> ToolResult:
        return ToolResult(success=True, data={"stdout": f"ran: {command}"})

    router.register(local_exec_shell)
    return router


@pytest.mark.asyncio
async def test_tool_call_result_event_carries_the_sandbox_marker() -> None:
    provider = make_provider(
        [
            {"tool_calls": [tc("local_exec_shell", {"command": "ls"})]},
            {"content": "done"},
        ]
    )
    loop = CoreLoop(
        provider,
        SandboxedToolExecutor(RouterToolExecutor(_router_with_echo_shell()), backend=None),
    )

    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    results = [e for e in events if e.kind == "tool_call_result"]
    assert len(results) == 1
    assert results[0].data["sandbox"] == {"enforcement": "none"}


@pytest.mark.asyncio
async def test_tool_call_result_event_omits_marker_for_plain_results() -> None:
    provider = make_provider(
        [
            {"tool_calls": [tc("local_exec_shell", {"command": "ls"})]},
            {"content": "done"},
        ]
    )
    loop = CoreLoop(provider, RouterToolExecutor(_router_with_echo_shell()))

    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    results = [e for e in events if e.kind == "tool_call_result"]
    assert len(results) == 1
    assert "sandbox" not in results[0].data
