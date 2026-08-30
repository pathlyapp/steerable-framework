from __future__ import annotations

import pytest
from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime.hooks import CompletionDraft
from steerable_agent_runtime.loop import LoopContext

from steerable_sidecar.delivery import DeliveryHooks, named_output_paths


def _call(name: str) -> ToolCall:
    return ToolCall(id="t", name=name, arguments={})


def _draft(*, tools: int, content: str = "done", had_tool_calls: bool | None = None) -> CompletionDraft:
    return CompletionDraft(
        status="completed",
        reason="stop",
        content=content,
        round_index=1,
        had_tool_calls=tools > 0 if had_tool_calls is None else had_tool_calls,
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
    assert "placeholders" in action.appends[0].message.content_text
    assert "prose description" in action.appends[0].message.content_text
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
    assert "drafted" in (first.message or "")
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
async def test_completion_accepts_after_bash_write() -> None:
    hooks = DeliveryHooks()
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(
        ok,
        ToolCall(
            id="t",
            name="bash",
            arguments={"command": 'python -c "open(\'/app/answer.txt\',\'w\').write(\'1\')"'},
        ),
        ctx,
    )
    action = await hooks.before_completion(_draft(tools=1), ctx)
    assert action.kind == "accept"
    assert hooks.writes == 1


@pytest.mark.asyncio
async def test_completion_accepts_after_python_script_or_make() -> None:
    hooks = DeliveryHooks()
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(
        ok,
        ToolCall(id="t", name="bash", arguments={"command": "python3 render.py"}),
        ctx,
    )
    assert hooks.writes == 1
    await hooks.post_tool_result(
        ok,
        ToolCall(id="t", name="bash", arguments={"command": "make -C /app all"}),
        ctx,
    )
    action = await hooks.before_completion(_draft(tools=2), ctx)
    assert action.kind == "accept"
    assert hooks.writes == 2


@pytest.mark.asyncio
async def test_explore_bash_does_not_count_as_write() -> None:
    hooks = DeliveryHooks()
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(
        ok, ToolCall(id="t", name="bash", arguments={"command": "ls /app && cat README"}), ctx
    )
    await hooks.post_tool_result(
        ok, ToolCall(id="t", name="bash", arguments={"command": "python -c 'print(2 > 1)'"}), ctx
    )
    first = await hooks.before_completion(_draft(tools=2), ctx)
    assert first.kind == "retry"
    assert first.reason == "no_artifact"


@pytest.mark.asyncio
async def test_single_tool_turn_does_not_retry() -> None:
    hooks = DeliveryHooks()
    action = await hooks.before_completion(_draft(tools=1), LoopContext())
    assert action.kind == "accept"


@pytest.mark.asyncio
async def test_empty_round_retries_before_accept() -> None:
    hooks = DeliveryHooks(max_empty_round_retries=2)
    ctx = LoopContext()
    draft = _draft(tools=2, content="", had_tool_calls=False)
    first = await hooks.before_completion(draft, ctx)
    assert first.kind == "retry"
    assert first.reason == "empty_round"
    second = await hooks.before_completion(draft, ctx)
    assert second.kind == "retry"
    assert second.reason == "empty_round"
    third = await hooks.before_completion(draft, ctx)
    assert third.kind == "retry"
    assert third.reason == "no_artifact"


@pytest.mark.asyncio
async def test_empty_round_retries_even_with_zero_tools() -> None:
    hooks = DeliveryHooks()
    action = await hooks.before_completion(
        _draft(tools=0, content="  ", had_tool_calls=False), LoopContext()
    )
    assert action.kind == "retry"
    assert action.reason == "empty_round"


@pytest.mark.asyncio
async def test_empty_round_forces_tool_choice_on_next_step() -> None:
    hooks = DeliveryHooks()
    ctx = LoopContext()
    await hooks.before_completion(
        _draft(tools=0, content="", had_tool_calls=False), ctx
    )
    action = await hooks.pre_step([], ctx)
    assert action.tool_choice == "required"
    assert action.reason == "empty_round_force_tool"
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("bash"), ctx)
    ctx.round_index = 1
    again = await hooks.pre_step([], ctx)
    assert again.tool_choice == "required"
    await hooks.post_tool_result(ok, _call("write_file"), ctx)
    done = await hooks.pre_step([], ctx)
    assert done.tool_choice is None


def test_named_output_paths_skips_usr_bin() -> None:
    paths = named_output_paths(
        "Write /app/re.json and /tmp/frame.bmp using /usr/bin/python3 "
        "and the existing /app/check.py"
    )
    assert paths == ("/app/re.json", "/tmp/frame.bmp", "/app/check.py")


@pytest.mark.asyncio
async def test_missing_named_output_retries_after_helper_write(
    tmp_path,
) -> None:
    target = tmp_path / "re.json"
    hooks = DeliveryHooks(named_outputs=(str(target),))
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("write_file"), ctx)
    first = await hooks.before_completion(_draft(tools=2), ctx)
    assert first.kind == "retry"
    assert first.reason == "missing_named_output"
    assert str(target) in (first.message or "")
    action = await hooks.pre_step([], ctx)
    assert action.tool_choice == "required"
    target.write_text("[]", encoding="utf-8")
    second = await hooks.before_completion(_draft(tools=3), ctx)
    assert second.kind == "accept"


@pytest.mark.asyncio
async def test_existing_named_path_is_input_not_required(tmp_path) -> None:
    existing = tmp_path / "check.py"
    existing.write_text("pass", encoding="utf-8")
    missing = tmp_path / "re.json"
    hooks = DeliveryHooks(named_outputs=(str(existing), str(missing)))
    ctx = LoopContext()
    first = await hooks.before_completion(_draft(tools=2), ctx)
    assert first.kind == "retry"
    assert first.reason == "missing_named_output"
    assert str(missing) in (first.message or "")
    assert str(existing) not in (first.message or "")


@pytest.mark.asyncio
async def test_missing_named_output_retries_more_than_once(tmp_path) -> None:
    target = tmp_path / "re.json"
    hooks = DeliveryHooks(named_outputs=(str(target),))
    ctx = LoopContext()
    first = await hooks.before_completion(_draft(tools=2), ctx)
    assert first.kind == "retry"
    second = await hooks.before_completion(_draft(tools=3), ctx)
    assert second.kind == "retry"
    assert second.reason == "missing_named_output"
    target.write_text("[]", encoding="utf-8")
    third = await hooks.before_completion(_draft(tools=4), ctx)
    assert third.kind == "accept"


@pytest.mark.asyncio
async def test_no_write_forces_tool_choice_until_first_write() -> None:
    hooks = DeliveryHooks()
    ctx = LoopContext()
    ctx.round_index = 0
    action = await hooks.pre_step([], ctx)
    assert action.tool_choice == "required"
    assert action.reason == "no_write_force_tool"
    ctx.round_index = 1
    later = await hooks.pre_step([], ctx)
    assert later.tool_choice == "required"
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("write_file"), ctx)
    ctx.round_index = 2
    done = await hooks.pre_step([], ctx)
    assert done.tool_choice is None
