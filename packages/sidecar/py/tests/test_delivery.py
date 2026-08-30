from __future__ import annotations

import pytest
from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime.hooks import CompletionDraft
from steerable_agent_runtime.loop import LoopContext

from steerable_sidecar.delivery import DeliveryHooks


def _call(name: str) -> ToolCall:
    return ToolCall(id="t", name=name, arguments={})


def _draft(*, tools: int) -> CompletionDraft:
    return CompletionDraft(
        status="completed",
        reason="stop",
        content="done",
        round_index=1,
        had_tool_calls=tools > 0,
        tool_calls_used=tools,
        tool_successes=tools,
    )


@pytest.mark.asyncio
async def test_nudge_after_explore_without_write() -> None:
    hooks = DeliveryHooks(explore_before_nudge=2)
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("bash"), ctx)
    await hooks.post_tool_result(ok, _call("read_file"), ctx)
    action = await hooks.pre_step([], ctx)
    assert action.kind == "proceed"
    assert action.appends
    assert action.append_action == "delivery_nudge"
    assert "write_file" in action.appends[0].message.content_text
    assert hooks.nudges == 1
    # Counter reset so the next step is not nudged immediately.
    again = await hooks.pre_step([], ctx)
    assert again.appends is None


@pytest.mark.asyncio
async def test_write_clears_explore_counter() -> None:
    hooks = DeliveryHooks(explore_before_nudge=2)
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("bash"), ctx)
    await hooks.post_tool_result(ok, _call("write_file"), ctx)
    await hooks.post_tool_result(ok, _call("bash"), ctx)
    action = await hooks.pre_step([], ctx)
    assert action.appends is None
    assert hooks.writes == 1


@pytest.mark.asyncio
async def test_completion_retry_without_artifacts() -> None:
    hooks = DeliveryHooks()
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("bash"), ctx)
    await hooks.post_tool_result(ok, _call("bash"), ctx)
    first = await hooks.before_completion(_draft(tools=2), ctx)
    assert first.kind == "retry"
    assert first.reason == "no_artifact"
    second = await hooks.before_completion(_draft(tools=2), ctx)
    assert second.kind == "accept"


@pytest.mark.asyncio
async def test_completion_accepts_after_write() -> None:
    hooks = DeliveryHooks()
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("edit_file"), ctx)
    action = await hooks.before_completion(_draft(tools=1), ctx)
    assert action.kind == "accept"


@pytest.mark.asyncio
async def test_single_tool_turn_does_not_retry() -> None:
    hooks = DeliveryHooks()
    action = await hooks.before_completion(_draft(tools=1), LoopContext())
    assert action.kind == "accept"
