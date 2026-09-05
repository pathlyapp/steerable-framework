from __future__ import annotations

import os
import socket
import threading
from pathlib import Path

import pytest
from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime.hooks import CompletionDraft
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.loop import LoopContext

from steerable_sidecar.delivery import (
    DeliveryGatedExecutor,
    DeliveryHooks,
    _UNVERIFIED_RETRY,
    _is_script_listener,
    instruction_example_commands,
    named_output_paths,
    named_socket_paths,
)


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


def test_default_explore_before_nudge() -> None:
    assert DeliveryHooks()._explore_before_nudge == 8


@pytest.mark.asyncio
async def test_named_missing_nudges_after_four_inspects(tmp_path) -> None:
    target = tmp_path / "solution.txt"
    hooks = DeliveryHooks(named_outputs=(str(target),))
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    for _ in range(3):
        await hooks.post_tool_result(ok, _call("bash"), ctx)
    early = await hooks.pre_step([], ctx)
    assert early.appends is None
    await hooks.post_tool_result(ok, _call("read_file"), ctx)
    action = await hooks.pre_step([], ctx)
    assert action.appends
    assert action.append_action == "delivery_nudge"
    assert hooks.nudges == 1
    assert str(target) in action.appends[0].message.content_text


@pytest.mark.asyncio
async def test_unnamed_still_waits_eight_inspects() -> None:
    hooks = DeliveryHooks()
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    for _ in range(7):
        await hooks.post_tool_result(ok, _call("bash"), ctx)
    early = await hooks.pre_step([], ctx)
    assert early.appends is None
    await hooks.post_tool_result(ok, _call("read_file"), ctx)
    action = await hooks.pre_step([], ctx)
    assert action.appends
    assert hooks.nudges == 1


@pytest.mark.asyncio
async def test_empty_named_file_still_nudges(tmp_path) -> None:
    target = tmp_path / "solution.txt"
    hooks = DeliveryHooks(named_outputs=(str(target),))
    target.write_text("", encoding="utf-8")
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    for _ in range(4):
        await hooks.post_tool_result(ok, _call("bash"), ctx)
    action = await hooks.pre_step([], ctx)
    assert action.appends
    assert hooks.writes == 0
    assert hooks.nudges == 1
    assert str(target) in action.appends[0].message.content_text


@pytest.mark.asyncio
async def test_empty_named_file_still_blocks_inspect(tmp_path) -> None:
    target = tmp_path / "solution.txt"
    hooks = DeliveryHooks(
        named_outputs=(str(target),),
        explore_before_nudge=1,
        max_nudges=1,
    )
    target.write_text("", encoding="utf-8")
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    for _ in range(2):
        await hooks.post_tool_result(ok, _call("bash"), ctx)
        nudged = await hooks.pre_step([], ctx)
        assert nudged.appends
    blocked = hooks.inspect_block_result(_call("read_file"))
    assert blocked is not None
    assert str(target) in (blocked.error or "")


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
    assert "reasoning" in action.appends[0].message.content_text
    assert hooks.nudges == 1
    # Counter reset so the next step is not nudged immediately.
    again = await hooks.pre_step([], ctx)
    assert again.appends is None


@pytest.mark.asyncio
async def test_named_missing_keeps_nudging_after_max_nudges(tmp_path) -> None:
    target = tmp_path / "primers.fasta"
    hooks = DeliveryHooks(
        named_outputs=(str(target),),
        explore_before_nudge=1,
        max_nudges=1,
    )
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("bash"), ctx)
    first = await hooks.pre_step([], ctx)
    assert first.appends
    assert hooks.nudges == 1
    await hooks.post_tool_result(ok, _call("bash"), ctx)
    second = await hooks.pre_step([], ctx)
    assert second.appends
    assert hooks.nudges == 2
    assert str(target) in second.appends[0].message.content_text
    assert "Still missing" in second.appends[0].message.content_text


@pytest.mark.asyncio
async def test_named_missing_explore_nudges_are_capped(tmp_path) -> None:
    target = tmp_path / "re.json"
    hooks = DeliveryHooks(
        named_outputs=(str(target),),
        explore_before_nudge=1,
        max_nudges=1,
    )
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    for _ in range(16):
        await hooks.post_tool_result(ok, _call("bash"), ctx)
        action = await hooks.pre_step([], ctx)
        assert action.appends
    await hooks.post_tool_result(ok, _call("bash"), ctx)
    stopped = await hooks.pre_step([], ctx)
    assert stopped.appends is None
    assert hooks.nudges == 16


@pytest.mark.asyncio
async def test_wrap_up_lists_missing_named_paths_once(tmp_path) -> None:
    dest = tmp_path / "out.txt"
    hooks = DeliveryHooks(
        instruction=f"write {dest}",
        named_outputs=(str(dest),),
    )
    notice = [
        LLMMessage.text_of(
            "user",
            "[system notice] The time budget for this task is nearly exhausted.",
        )
    ]
    first = await hooks.pre_step(notice, LoopContext())
    assert first.reason == "wrap_up_named_output"
    assert first.tool_choice == "required"
    assert str(dest) in (first.appends[0].message.content_text or "")
    assert f"cat > {dest} <<'EOF'" in (first.appends[0].message.content_text or "")
    second = await hooks.pre_step(notice, LoopContext())
    assert second.reason != "wrap_up_named_output"


@pytest.mark.asyncio
async def test_wrap_up_shown_text_when_named_txt_is_a_raster(tmp_path) -> None:
    dest = tmp_path / "out.txt"
    dest.write_bytes(b"#" * 5000)
    hooks = DeliveryHooks(
        instruction=(
            "When I run the print, what will the text show? "
            f"Write the output to {dest}."
        ),
        named_outputs=(str(dest),),
    )
    notice = [
        LLMMessage.text_of(
            "user",
            "[system notice] The time budget for this task is nearly exhausted.",
        )
    ]
    first = await hooks.pre_step(notice, LoopContext())
    assert first.reason == "wrap_up_shown_text"
    assert first.tool_choice == "required"
    assert str(dest) in (first.appends[0].message.content_text or "")
    second = await hooks.pre_step(notice, LoopContext())
    assert second.reason != "wrap_up_shown_text"
    dest.write_text("flag{short}\n", encoding="utf-8")
    assert hooks.wrap_up_may_drop_tools() is True


def test_wrap_up_keeps_tools_while_shown_text_is_a_raster(tmp_path) -> None:
    dest = tmp_path / "out.txt"
    dest.write_bytes(b"." * 8000)
    hooks = DeliveryHooks(
        instruction=(
            "When I run the print, what will the text show? "
            f"Write the output to {dest}."
        ),
        named_outputs=(str(dest),),
    )
    assert hooks.wrap_up_may_drop_tools() is False
    dest.write_text("HELLO\n", encoding="utf-8")
    assert hooks.wrap_up_may_drop_tools() is True


@pytest.mark.asyncio
async def test_inspect_blocked_after_wrap_up_shown_text(tmp_path) -> None:
    dest = tmp_path / "out.txt"
    dest.write_bytes(b"#" * 5000)
    hooks = DeliveryHooks(
        instruction=(
            "When I run the print, what will the text show? "
            f"Write the output to {dest}."
        ),
        named_outputs=(str(dest),),
    )
    notice = [
        LLMMessage.text_of(
            "user",
            "[system notice] The time budget for this task is nearly exhausted.",
        )
    ]
    inspect = ToolCall(id="t", name="bash", arguments={"command": f"cat {dest}"})
    assert hooks.inspect_block_result(inspect) is None
    first = await hooks.pre_step(notice, LoopContext())
    assert first.reason == "wrap_up_shown_text"
    blocked = hooks.inspect_block_result(inspect)
    assert blocked is not None
    assert str(dest) in (blocked.error or "")
    write = ToolCall(
        id="t",
        name="bash",
        arguments={"command": f"cat > {dest} <<'EOF'\nHI\nEOF"},
    )
    assert hooks.inspect_block_result(write) is None
    dump_read = ToolCall(
        id="t", name="read_file", arguments={"path": str(dest)}
    )
    assert hooks.inspect_block_result(dump_read) is not None


@pytest.mark.asyncio
async def test_wrap_up_shown_text_when_dump_has_a_caption(tmp_path) -> None:
    dest = tmp_path / "out.txt"
    dest.write_text(
        "=== VIEW (XY) of object at z=0.48 ===\n" + ("." * 80 + "\n") * 10,
        encoding="utf-8",
    )
    hooks = DeliveryHooks(
        instruction=(
            "When I run the print, what will the text show? "
            f"Write the output to {dest}."
        ),
        named_outputs=(str(dest),),
    )
    notice = [
        LLMMessage.text_of(
            "user",
            "[system notice] The time budget for this task is nearly exhausted.",
        )
    ]
    first = await hooks.pre_step(notice, LoopContext())
    assert first.reason == "wrap_up_shown_text"
    assert first.tool_choice == "required"


@pytest.mark.asyncio
async def test_inspect_blocks_helper_rewrite_after_wrap_up_shown(tmp_path) -> None:
    dest = tmp_path / "out.txt"
    helper = tmp_path / "parse.py"
    dest.write_bytes(b"#" * 5000)
    helper.write_text(
        f"from pathlib import Path\nPath({str(dest)!r}).write_text('dump')\n",
        encoding="utf-8",
    )
    hooks = DeliveryHooks(
        instruction=(
            "When I run the print, what will the text show? "
            f"Write the output to {dest}."
        ),
        named_outputs=(str(dest),),
    )
    notice = [
        LLMMessage.text_of(
            "user",
            "[system notice] The time budget for this task is nearly exhausted.",
        )
    ]
    rerun = ToolCall(
        id="t", name="bash", arguments={"command": f"python3 {helper}"}
    )
    assert hooks.inspect_block_result(rerun) is None
    first = await hooks.pre_step(notice, LoopContext())
    assert first.reason == "wrap_up_shown_text"
    assert hooks.inspect_block_result(rerun) is not None
    redirect = ToolCall(
        id="t",
        name="bash",
        arguments={"command": f"python3 {helper} > {dest}"},
    )
    assert hooks.inspect_block_result(redirect) is not None
    inline = ToolCall(
        id="t",
        name="bash",
        arguments={
            "command": f"python3 -c \"open({str(dest)!r},'w').write('HI\\n')\""
        },
    )
    assert hooks.inspect_block_result(inline) is None


@pytest.mark.asyncio
async def test_wrap_up_skips_named_nudge_when_outputs_exist(tmp_path) -> None:
    dest = tmp_path / "out.txt"
    dest.write_text("ok\n", encoding="utf-8")
    hooks = DeliveryHooks(
        instruction=f"write {dest}",
        named_outputs=(str(dest),),
    )
    notice = [
        LLMMessage.text_of(
            "user",
            "[system notice] The time budget for this task is nearly exhausted.",
        )
    ]
    action = await hooks.pre_step(notice, LoopContext())
    assert action.reason != "wrap_up_named_output"


@pytest.mark.asyncio
async def test_wrap_up_skips_named_nudge_when_required_path_is_a_directory(
    tmp_path,
) -> None:
    dest = tmp_path / "povray-2.2"
    hooks = DeliveryHooks(
        instruction=f"compile the 2.2 tree in {dest}",
        named_outputs=(str(dest),),
    )
    dest.mkdir()
    (dest / "BUILD-NOTES.md").write_text("ok\n", encoding="utf-8")
    notice = [
        LLMMessage.text_of(
            "user",
            "[system notice] The time budget for this task is nearly exhausted.",
        )
    ]
    action = await hooks.pre_step(notice, LoopContext())
    assert action.reason != "wrap_up_named_output"


@pytest.mark.asyncio
async def test_named_directory_counts_as_a_write(tmp_path) -> None:
    dest = tmp_path / "povray-2.2"
    hooks = DeliveryHooks(named_outputs=(str(dest),))
    dest.mkdir()
    await hooks.post_tool_result(
        ToolResult(success=True, data={}), _call("bash"), LoopContext()
    )
    assert hooks.writes >= 1


@pytest.mark.asyncio
async def test_inspect_blocked_after_wrap_up_named(tmp_path) -> None:
    dest = tmp_path / "out.txt"
    hooks = DeliveryHooks(
        instruction=f"write {dest}",
        named_outputs=(str(dest),),
    )
    notice = [
        LLMMessage.text_of(
            "user",
            "[system notice] The time budget for this task is nearly exhausted.",
        )
    ]
    inspect = ToolCall(id="t", name="bash", arguments={"command": "ls /app"})
    assert hooks.inspect_block_result(inspect) is None
    first = await hooks.pre_step(notice, LoopContext())
    assert first.reason == "wrap_up_named_output"
    blocked = hooks.inspect_block_result(inspect)
    assert blocked is not None
    assert str(dest) in (blocked.error or "")
    write = ToolCall(
        id="t", name="bash", arguments={"command": f"cat > {dest}"}
    )
    assert hooks.inspect_block_result(write) is None
    source = tmp_path / "doomgeneric_img.c"
    source.write_text("void DG_DrawFrame(void) {}\n", encoding="utf-8")
    read_ok = ToolCall(
        id="t", name="read_file", arguments={"path": str(source)}
    )
    assert hooks.inspect_block_result(read_ok) is None
    read_missing = ToolCall(
        id="t", name="read_file", arguments={"path": str(tmp_path / "absent.c")}
    )
    assert hooks.inspect_block_result(read_missing) is not None
    cat_ok = ToolCall(
        id="t", name="bash", arguments={"command": f"cat {source}"}
    )
    assert hooks.inspect_block_result(cat_ok) is None
    cat_missing = ToolCall(
        id="t",
        name="bash",
        arguments={"command": f"cat {tmp_path / 'absent.c'}"},
    )
    assert hooks.inspect_block_result(cat_missing) is not None


@pytest.mark.asyncio
async def test_nudge_after_compact_without_write() -> None:
    hooks = DeliveryHooks(explore_before_nudge=20)
    ctx = LoopContext()
    transcript = [
        LLMMessage.text_of(
            "user",
            "[context compacted: earlier conversation summarized]\nexplored ELF",
        )
    ]
    action = await hooks.pre_step(transcript, ctx)
    assert action.appends
    assert action.append_action == "delivery_nudge"
    assert hooks.nudges == 1
    again = await hooks.pre_step(transcript, ctx)
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
    assert "truncate" in (first.message or "")
    assert "bash" in (first.message or "")
    second = await hooks.before_completion(_draft(tools=2), ctx)
    assert second.kind == "accept"


@pytest.mark.asyncio
async def test_completion_accepts_a_write_once_it_has_been_checked() -> None:
    """A bare write is refused once; an inspect call afterwards accepts it.

    Stopping on the write means nothing ran against the output. Two catalog-89
    runs put those trials at a 0.647 pass rate against 0.775 for trials that
    ran one or two calls after their last write.
    """
    hooks = DeliveryHooks()
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("edit_file"), ctx)
    refused = await hooks.before_completion(_draft(tools=1), ctx)
    assert refused.kind == "retry"
    assert refused.reason == "unverified_output"
    await hooks.post_tool_result(
        ok,
        ToolCall(id="t", name="bash", arguments={"command": "pytest -q"}),
        ctx,
    )
    assert (await hooks.before_completion(_draft(tools=2), ctx)).kind == "accept"


@pytest.mark.asyncio
async def test_unverified_gate_refuses_only_once() -> None:
    """A model that ignores the retry is not held past one attempt.

    The budget buys the step from no check to a check. Trials that kept going
    for eleven or more calls after their last write passed at 0.000, so a
    larger budget here would fund thrashing.
    """
    hooks = DeliveryHooks()
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("edit_file"), ctx)
    assert (await hooks.before_completion(_draft(tools=1), ctx)).kind == "retry"
    assert (await hooks.before_completion(_draft(tools=1), ctx)).kind == "accept"


@pytest.mark.asyncio
async def test_unverified_gate_stands_down_during_wrap_up() -> None:
    """Once the budget notice has been seen, take the delivery over a check.

    A refused completion buys another round, and in wrap-up that round can be
    the one Harbor kills, losing a written artifact to check a written
    artifact.
    """
    hooks = DeliveryHooks()
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.pre_step(
        [LLMMessage.text_of("user", "The time budget for this task is almost up")],
        ctx,
    )
    await hooks.post_tool_result(ok, _call("edit_file"), ctx)
    assert (await hooks.before_completion(_draft(tools=1), ctx)).kind == "accept"


@pytest.mark.asyncio
async def test_unverified_gate_refuses_a_turn_that_only_looked_at_its_output() -> None:
    """Viewing the output does not stand the gate down, however compound.

    This is the case the gate missed while it asked whether the write was the
    last action: circuit-fibsqrt ended on `cat gates.txt | head -50; wc -l`,
    counting the lines of a file it never sent to the circuit evaluator. Of
    the 56 catalog-89 trials that ended on a non-writing bash, the 15 that
    only viewed passed at 0.533 against 0.727 for those that ran a program.
    """
    hooks = DeliveryHooks()
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("edit_file"), ctx)
    await hooks.post_tool_result(
        ok,
        ToolCall(
            id="t",
            name="bash",
            arguments={"command": "cd /app && cat gates.txt | head -50; wc -l gates.txt"},
        ),
        ctx,
    )
    action = await hooks.before_completion(_draft(tools=2), ctx)
    assert action.kind == "retry"
    assert action.reason == "unverified_output"


@pytest.mark.asyncio
async def test_unverified_gate_treats_a_blind_sed_as_no_check() -> None:
    """An in-place edit leaves the output no more verified than before it."""
    hooks = DeliveryHooks()
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("write_file"), ctx)
    await hooks.post_tool_result(
        ok,
        ToolCall(
            id="t",
            name="bash",
            arguments={"command": "sed -i 's/0.93/0.995/' results.json"},
        ),
        ctx,
    )
    action = await hooks.before_completion(_draft(tools=2), ctx)
    assert action.reason == "unverified_output"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "./solver --self-check",
        "cd /app && ./sim gates.txt",
        "echo score=$(./score --report)",
        "cat input.txt | ./filter",
        "uv run pytest tests/",
    ],
)
async def test_unverified_gate_stands_down_once_anything_ran(command: str) -> None:
    """Any word outside the viewing list counts as a run, known or not.

    The gate can only be wrong by staying quiet, which is why an unrecognised
    command is read as a run: a list of the ways to check an output would go
    stale, while the ways to look at one are a closed set.
    """
    hooks = DeliveryHooks()
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("edit_file"), ctx)
    await hooks.post_tool_result(
        ok, ToolCall(id="t", name="bash", arguments={"command": command}), ctx
    )
    assert (await hooks.before_completion(_draft(tools=2), ctx)).kind == "accept"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    ["cat input.txt | ./filter > out.txt", "python3 score.py"],
)
async def test_unverified_gate_holds_a_command_that_ran_and_also_wrote(
    command: str,
) -> None:
    """Delivering while running leaves the delivery itself unchecked.

    Whatever such a command executed, it ran before the bytes it produced
    existed, so those bytes are in the state the gate exists to refuse. This
    is also why ``_bash_writes`` counting `python3 x.py` as a write does not
    need to distinguish a generator from a scorer here.
    """
    hooks = DeliveryHooks()
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(
        ok, ToolCall(id="t", name="bash", arguments={"command": command}), ctx
    )
    action = await hooks.before_completion(_draft(tools=1), ctx)
    assert action.reason == "unverified_output"


@pytest.mark.asyncio
async def test_unverified_gate_reopens_when_the_fix_lands_after_the_check() -> None:
    """Running, then editing, is as unverified as never running at all."""
    hooks = DeliveryHooks()
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("write_file"), ctx)
    await hooks.post_tool_result(
        ok, ToolCall(id="t", name="bash", arguments={"command": "pytest -q"}), ctx
    )
    await hooks.post_tool_result(ok, _call("edit_file"), ctx)
    action = await hooks.before_completion(_draft(tools=3), ctx)
    assert action.reason == "unverified_output"


@pytest.mark.asyncio
async def test_unverified_gate_stays_out_of_a_trial_with_no_write() -> None:
    """An inspect-only turn is the no_artifact retry's business, not this gate."""
    hooks = DeliveryHooks()
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("read_file"), ctx)
    action = await hooks.before_completion(_draft(tools=1), ctx)
    assert action.reason != "unverified_output"


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
    assert action.reason == "unverified_output"
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
    assert action.reason == "unverified_output"
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
    await hooks.post_tool_result(
        ok,
        ToolCall(
            id="t",
            name="bash",
            arguments={"command": "qemu-system-x86_64 -daemonize -cdrom /app/alpine.iso"},
        ),
        ctx,
    )
    first = await hooks.before_completion(_draft(tools=3), ctx)
    assert first.kind == "retry"
    assert first.reason == "no_artifact"


@pytest.mark.asyncio
async def test_single_tool_turn_does_not_retry() -> None:
    hooks = DeliveryHooks()
    action = await hooks.before_completion(_draft(tools=1), LoopContext())
    assert action.kind == "accept"


@pytest.mark.asyncio
async def test_missing_named_output_beats_empty_round(tmp_path) -> None:
    target = tmp_path / "out.txt"
    hooks = DeliveryHooks(named_outputs=(str(target),), max_empty_round_retries=6)
    action = await hooks.before_completion(
        _draft(tools=2, content="", had_tool_calls=False), LoopContext()
    )
    assert action.kind == "retry"
    assert action.reason == "missing_named_output"
    assert str(target) in (action.message or "")
    assert f"cat > {target} <<'EOF'" in (action.message or "")


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


@pytest.mark.asyncio
async def test_livelock_write_now_keeps_required_until_a_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """In-flight wrap-up livelock fired WRITE_NOW then dropped tools
    (starter named file exists) and stopped requiring, so the model
    thought until hard_timeout. Keep tools and required until a write."""
    monkeypatch.setenv("STEERABLE_LIVELOCK_EMPTY_STREAK", "3")
    dest = tmp_path / "gates.txt"
    dest.write_text("starter\n", encoding="utf-8")
    hooks = DeliveryHooks(named_outputs=(str(dest),))
    ctx = LoopContext()
    wrap = [
        LLMMessage.text_of("user", "The time budget for this task is almost gone.")
    ]
    await hooks.before_completion(
        _draft(tools=0, content="", had_tool_calls=False), ctx
    )
    first = await hooks.pre_step(wrap, ctx)
    assert first.tool_choice == "required"
    second = await hooks.pre_step(wrap, ctx)
    assert second.tool_choice == "required"
    third = await hooks.pre_step(wrap, ctx)
    assert third.tool_choice == "required"
    assert third.reason == "forced_empty_livelock"
    assert third.appends
    assert "Forced tool rounds" in (third.appends[0].message.content_text or "")
    assert hooks.wrap_up_may_drop_tools() is False
    fourth = await hooks.pre_step(wrap, ctx)
    assert fourth.tool_choice == "required"
    assert fourth.appends is None
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("write_file"), ctx)
    fifth = await hooks.pre_step(wrap, ctx)
    assert fifth.tool_choice is None
    assert fifth.appends is None


@pytest.mark.asyncio
async def test_livelock_keeps_required_when_writes_already_happened(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Catalog 70 wrote a wrong digest before wrap-up; ``writes > 0``
    must not drop required while the stated prefix still misses."""
    monkeypatch.setenv("STEERABLE_LIVELOCK_EMPTY_STREAK", "3")
    dest = tmp_path / "output.txt"
    dest.write_text(
        "39ad5ff920409f58e961498c997af185ff1b68241c83bcac8ce67a30c05d28c5\n",
        encoding="utf-8",
    )
    hooks = DeliveryHooks(
        instruction=_CODE_FROM_IMAGE.format(dest=dest),
        named_outputs=(str(dest),),
    )
    ctx = LoopContext()
    wrap = [
        LLMMessage.text_of("user", "The time budget for this task is almost gone.")
    ]
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("write_file"), ctx)
    assert hooks.writes >= 1
    first = await hooks.pre_step(wrap, ctx)
    assert first.reason == "wrap_up_named_prefix"
    second = await hooks.pre_step(wrap, ctx)
    assert second.tool_choice == "required"
    third = await hooks.pre_step(wrap, ctx)
    assert third.tool_choice == "required"
    fourth = await hooks.pre_step(wrap, ctx)
    assert fourth.reason == "forced_empty_livelock"
    assert fourth.tool_choice == "required"
    later = await hooks.pre_step(wrap, ctx)
    assert later.tool_choice == "required"
    dest.write_text("bee26a0ff\n", encoding="utf-8")
    done = await hooks.pre_step(wrap, ctx)
    assert done.tool_choice is None


@pytest.mark.asyncio
async def test_livelock_write_now_on_pre_wrap_empty_with_no_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEERABLE_LIVELOCK_EMPTY_STREAK", "3")
    hooks = DeliveryHooks()
    ctx = LoopContext()
    await hooks.before_completion(
        _draft(tools=0, content="", had_tool_calls=False), ctx
    )
    first = await hooks.pre_step([], ctx)
    assert first.tool_choice == "required"
    assert first.reason != "forced_empty_livelock"
    second = await hooks.pre_step([], ctx)
    assert second.tool_choice == "required"
    third = await hooks.pre_step([], ctx)
    assert third.tool_choice == "required"
    assert third.reason == "forced_empty_livelock"


@pytest.mark.asyncio
async def test_livelock_does_not_count_pre_wrap_empty_after_a_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEERABLE_LIVELOCK_EMPTY_STREAK", "3")
    hooks = DeliveryHooks()
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("write_file"), ctx)
    await hooks.before_completion(
        _draft(tools=0, content="", had_tool_calls=False), ctx
    )
    for _ in range(4):
        action = await hooks.pre_step([], ctx)
        assert action.tool_choice == "required"
        assert action.reason != "forced_empty_livelock"


@pytest.mark.asyncio
async def test_unverified_gate_disabled_by_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEERABLE_DELIVERY_VERIFY", "0")
    hooks = DeliveryHooks()
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("edit_file"), ctx)
    action = await hooks.before_completion(_draft(tools=1), ctx)
    assert action.reason != "unverified_output"


def test_unverified_retry_does_not_rerun_the_generator() -> None:
    """extract-elf Arm A: the old copy named the hidden-test command, which
    is `node extract.js > out.json` — deleting and regenerating the scored
    file. Two of three such trials then failed; Arm B (gate off) was 3/3."""
    assert "Do not rewrite or delete the scored files" in _UNVERIFIED_RETRY
    assert "Re-running the generator" in _UNVERIFIED_RETRY
    assert "hidden tests will execute" not in _UNVERIFIED_RETRY


def test_named_output_paths_skips_usr_bin() -> None:
    paths = named_output_paths(
        "Write /app/re.json and /tmp/frame.bmp using /usr/bin/python3 "
        "and the existing /app/check.py"
    )
    assert paths == ("/app/re.json", "/tmp/frame.bmp", "/app/check.py")


def test_named_output_paths_skips_unix_sockets() -> None:
    text = (
        "Write /app/re.json. Configure a QEMU monitor socket at "
        "`/tmp/qemu-monitor.sock` for programmatic keyboard input."
    )
    assert "/tmp/qemu-monitor.sock" not in named_output_paths(text)
    assert "/app/re.json" in named_output_paths(text)
    assert named_socket_paths(text) == ("/tmp/qemu-monitor.sock",)


def test_named_output_paths_keeps_nested_extensionless_binary() -> None:
    paths = named_output_paths(
        "Build /app/polyglot/cmain after cloning Caffe into /app/caffe. "
        "Also write /app/result.txt."
    )
    assert "/app/polyglot/cmain" in paths
    assert "/app/result.txt" in paths
    assert "/app/caffe" not in paths


def test_named_output_paths_reads_a_checklist_not_only_a_verb_phrase() -> None:
    """An output stated as something the tests confirm is still an output.

    Six stable reds had their hidden test report the required file simply
    absent, and four of them state the requirement this way rather than as
    "write a file X" — so the completion gate that exists to refuse exactly
    that ending never knew there was a path to wait for. Phrasing here is
    taken verbatim from those instructions.
    """
    assert "/app/image.c" in named_output_paths(
        "1. **File Existence**: `image.c` must exist\n"
        "2. **Compilation**: Code must compile with `gcc -o image image.c -lm`"
    )
    assert "/app/primers.fasta" in named_output_paths(
        "1. `primers.fasta` exists and contains exactly 8 primer pairs"
    )
    assert "/app/my_warrior.red" in named_output_paths(
        "2. **Warrior Exists**: Confirms `my_warrior.red` was created"
    )


def test_named_output_paths_reads_every_name_in_an_output_list() -> None:
    """A list of outputs is a list, and the hidden test wants all of them.

    cobol-modernization names three files in one parenthesised list and is
    scored on all three, so stopping at the first would leave the gate
    satisfied while two were still missing. The run cannot end at a full stop
    because the names contain one.
    """
    paths = named_output_paths(
        "- The output files (`ACCOUNTS.DAT`, `BOOKS.DAT`, `TRANSACTIONS.DAT`) "
        "must match byte-for-byte"
    )
    assert paths == ("/app/ACCOUNTS.DAT", "/app/BOOKS.DAT", "/app/TRANSACTIONS.DAT")


def test_named_output_paths_ignores_backticked_files_that_assert_nothing() -> None:
    """Selectivity is the whole reason this keys on the assertion.

    Treating every backticked filename as an output was measured over the 89
    catalog instructions: it adds 105 candidates across 49 of the 58 passing
    tasks, among them the hidden test file itself and `np.float64`, which is
    not a file at all. Those would become paths the gate waits for on trials
    that are already correct.
    """
    paths = named_output_paths(
        "The graders run `test_outputs.py` against your work. Cast with "
        "`np.float64` rather than `np.float`, and read the reference image "
        "`chess_board.png` from the working directory."
    )
    assert paths == ()


def test_named_output_paths_called_entrypoint_without_app_prefix() -> None:
    mips = named_output_paths(
        "implement a MIPS interpreter called vm.js so that I can run "
        "`node vm.js` and this should run the MIPS file"
    )
    assert "/app/vm.js" in mips
    doom = named_output_paths(
        "vm.js will expect a file called doomgeneric_mips and will run it, "
        "so that I can run `node vm.js`."
    )
    assert "/app/doomgeneric_mips" in doom
    assert "/app/vm.js" in doom
    quoted_elf = named_output_paths(
        "producing an ELF called `doomgeneric_mips`, run `node vm.js`, "
        "frames written to /tmp/frame.bmp"
    )
    assert "/app/doomgeneric_mips" in quoted_elf
    assert "/tmp/frame.bmp" in quoted_elf
    # Real TB instruction.md: the hidden test requires /tmp/frame.bmp, the
    # instruction never names that path. Domain notes already mention it.
    # Do not invent it from "first frame is correctly created and saved".
    real_mips = named_output_paths(
        "I have provided /app/doomgeneric_mips, a MIPS elf file, along with "
        "doomgeneric/, the corresponding source code. Please implement a "
        "MIPS interpreter complete with handling system calls called vm.js "
        "so that I can run `node vm.js` and this should run the MIPS file "
        "and read/write any files as necessary. Running this file should "
        "result in saving the frames as they are rendered, one at a time. "
        "I will check that you booted doom correctly, and check that the "
        "first frame is correctly created and saved."
    )
    assert "/app/vm.js" in real_mips
    assert "/tmp/frame.bmp" not in real_mips
    titled = named_output_paths(
        "Output the primers in a fasta file titled primers.fasta."
    )
    assert "/app/primers.fasta" in titled
    named = named_output_paths(
        "You must write a python script in /app named convert_masks.py that "
        "takes argparse args. Demo files: /app/demo_rgb.png and "
        "/app/demo_metadata.csv"
    )
    assert "/app/convert_masks.py" in named
    eval_scm = named_output_paths(
        "Write a file eval.scm that is a metacircular evaluator. "
        "Run `python3 interp.py eval.scm`."
    )
    assert "/app/eval.scm" in eval_scm
    warrior = named_output_paths(
        "Write your program to `my_warrior.red`. Do not modify files in "
        "`warriors/`."
    )
    assert "/app/my_warrior.red" in warrior
    cli = named_output_paths(
        'The tool should be a binary executable called "cli_tool" and a '
        'file called "prediction.txt".'
    )
    assert "/app/cli_tool" in cli
    assert "/app/prediction.txt" in cli
    gcode = named_output_paths(
        "When I run the print, what will the text show? Write the output "
        "to /app/out.txt"
    )
    assert "/app/out.txt" in gcode
    extract = named_output_paths(
        "create a file /app/solution.txt that has all the moves they input"
    )
    assert "/app/solution.txt" in extract
    steal = named_output_paths(
        "write a file called `/app/steal.py` that when run will output a "
        "matrix. The script should save this matrix to `/app/stolen_A1.npy`."
    )
    assert "/app/steal.py" in steal
    assert "/app/stolen_A1.npy" in steal
    tracing = named_output_paths(
        "I've put an image at /app/image.ppm. Write a c program image.c. "
        "Your output should be to a new file reconstructed.ppm in the cwd. "
        "I will test it by calling gcc -static -o image image.c -lm && ./image."
    )
    assert "/app/image.c" in tracing
    assert "/app/reconstructed.ppm" in tracing
    assert "/app/image.ppm" in tracing


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
async def test_missing_named_output_retries_past_old_eight_cap(tmp_path) -> None:
    target = tmp_path / "re.json"
    hooks = DeliveryHooks(named_outputs=(str(target),))
    ctx = LoopContext()
    for _ in range(9):
        action = await hooks.before_completion(_draft(tools=2), ctx)
        assert action.kind == "retry"
        assert action.reason == "missing_named_output"


@pytest.mark.asyncio
async def test_missing_named_output_retries_match_completion_redos(tmp_path) -> None:
    target = tmp_path / "primers.fasta"
    hooks = DeliveryHooks(named_outputs=(str(target),))
    ctx = LoopContext()
    for i in range(32):
        action = await hooks.before_completion(_draft(tools=2), ctx)
        assert action.kind == "retry", i
        assert action.reason == "missing_named_output"
    done = await hooks.before_completion(_draft(tools=2), ctx)
    assert done.kind == "accept"


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


@pytest.mark.asyncio
async def test_helper_write_does_not_count_as_named_delivery(tmp_path) -> None:
    target = tmp_path / "re.json"
    hooks = DeliveryHooks(named_outputs=(str(target),))
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("write_file"), ctx)
    await hooks.post_tool_result(
        ok,
        ToolCall(id="t", name="bash", arguments={"command": "python3 gen.py"}),
        ctx,
    )
    assert hooks.writes == 0
    action = await hooks.pre_step([], ctx)
    assert action.tool_choice == "required"
    assert action.reason == "no_write_force_tool"
    target.write_text("[]", encoding="utf-8")
    await hooks.post_tool_result(ok, _call("bash"), ctx)
    assert hooks.writes == 1
    done = await hooks.pre_step([], ctx)
    assert done.tool_choice is None


@pytest.mark.asyncio
async def test_inspect_blocked_after_named_nudges(tmp_path) -> None:
    target = tmp_path / "steal.py"
    hooks = DeliveryHooks(
        named_outputs=(str(target),),
        explore_before_nudge=1,
        max_nudges=1,
    )
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    inspect = ToolCall(id="t", name="bash", arguments={"command": "ls /app"})
    assert hooks.inspect_block_result(inspect) is None
    for _ in range(2):
        await hooks.post_tool_result(ok, _call("bash"), ctx)
        action = await hooks.pre_step([], ctx)
        assert action.appends
    blocked = hooks.inspect_block_result(inspect)
    assert blocked is not None
    assert blocked.success is False
    assert str(target) in (blocked.error or "")
    write = ToolCall(id="t", name="bash", arguments={"command": f"cat > {target}"})
    assert hooks.inspect_block_result(write) is None
    assert hooks.inspect_block_result(_call("write_file")) is None
    ffmpeg = ToolCall(
        id="t",
        name="bash",
        arguments={"command": "ffmpeg -i /app/video.mp4 /tmp/frame_%04d.png"},
    )
    assert hooks.inspect_block_result(ffmpeg) is not None
    ffmpeg_write = ToolCall(
        id="t",
        name="bash",
        arguments={"command": f"ffmpeg -i /app/v.mp4 -f rawvideo - > {target}"},
    )
    assert hooks.inspect_block_result(ffmpeg_write) is None
    helper = ToolCall(id="t", name="bash", arguments={"command": "python3 gen.py"})
    assert hooks.inspect_block_result(helper) is not None
    scratch = ToolCall(
        id="t",
        name="bash",
        arguments={"command": "cat > /tmp/explore.py << 'EOF'\nprint(1)\nEOF"},
    )
    assert hooks.inspect_block_result(scratch) is not None
    run_missing = ToolCall(
        id="t",
        name="bash",
        arguments={"command": f"python3 {target}"},
    )
    assert hooks.inspect_block_result(run_missing) is not None
    compile_elf = ToolCall(
        id="t",
        name="bash",
        arguments={"command": "make -C /app all"},
    )
    assert hooks.inspect_block_result(compile_elf) is None
    run_node = ToolCall(
        id="t",
        name="bash",
        arguments={"command": "timeout 60 node /app/vm.js"},
    )
    assert hooks.inspect_block_result(run_node) is None
    python_write = ToolCall(
        id="t",
        name="bash",
        arguments={
            "command": f"python3 -c \"open('{target}','w').write('x')\""
        },
    )
    assert hooks.inspect_block_result(python_write) is None
    sock = tmp_path / "qemu-monitor"
    other = DeliveryHooks(named_outputs=(str(sock),), explore_before_nudge=1)
    other.nudges = 4
    assert other.inspect_block_result(inspect) is None
    target.write_text("x", encoding="utf-8")
    assert hooks.inspect_block_result(inspect) is None


@pytest.mark.asyncio
async def test_inspect_allows_running_existing_named_script(tmp_path) -> None:
    script = tmp_path / "steal.py"
    npy = tmp_path / "stolen_A1.npy"
    hooks = DeliveryHooks(
        named_outputs=(str(script), str(npy)),
        explore_before_nudge=1,
        max_nudges=1,
    )
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    for _ in range(2):
        await hooks.post_tool_result(ok, _call("bash"), ctx)
        action = await hooks.pre_step([], ctx)
        assert action.appends
    run_missing = ToolCall(
        id="t",
        name="bash",
        arguments={"command": f"python3 {script}"},
    )
    assert hooks.inspect_block_result(run_missing) is not None
    script.write_text("print(1)\n", encoding="utf-8")
    run_existing = ToolCall(
        id="t",
        name="bash",
        arguments={"command": f"python3 {script}"},
    )
    assert hooks.inspect_block_result(run_existing) is None
    helper = tmp_path / "explore.py"
    helper.write_text("print(1)\n", encoding="utf-8")
    run_helper = ToolCall(
        id="t",
        name="bash",
        arguments={"command": f"python3 {helper}"},
    )
    assert hooks.inspect_block_result(run_helper) is not None


@pytest.mark.asyncio
async def test_inspect_allows_helper_python_that_writes_named_output(
    tmp_path,
) -> None:
    dest = tmp_path / "out.txt"
    helper = tmp_path / "parse.py"
    helper.write_text(
        f"open({str(dest)!r}, 'w').write('Hello')\n",
        encoding="utf-8",
    )
    hooks = DeliveryHooks(
        named_outputs=(str(dest),),
        explore_before_nudge=1,
        max_nudges=1,
    )
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    for _ in range(2):
        await hooks.post_tool_result(ok, _call("bash"), ctx)
        action = await hooks.pre_step([], ctx)
        assert action.appends
    run_helper = ToolCall(
        id="t",
        name="bash",
        arguments={"command": f"python3 {helper}"},
    )
    assert hooks.inspect_block_result(run_helper) is None
    other = tmp_path / "explore.py"
    other.write_text("print(open('/app/text.gcode').read()[:100])\n")
    run_other = ToolCall(
        id="t",
        name="bash",
        arguments={"command": f"python3 {other}"},
    )
    assert hooks.inspect_block_result(run_other) is not None


@pytest.mark.asyncio
async def test_gated_executor_does_not_run_blocked_inspect(tmp_path) -> None:
    target = tmp_path / "out.txt"
    hooks = DeliveryHooks(named_outputs=(str(target),))
    hooks.nudges = 4
    ran: list[str] = []

    class _Inner:
        async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
            ran.append(call.name)
            return ToolResult(success=True, data={"ran": True})

    gated = DeliveryGatedExecutor(_Inner(), hooks)
    ctx = LoopContext()
    inspect = ToolCall(id="t", name="read_file", arguments={"path": "/app/x"})
    result = await gated.execute(inspect, ctx)
    assert result.success is False
    assert ran == []
    ok = await gated.execute(_call("write_file"), ctx)
    assert ok.success is True
    assert ran == ["write_file"]


def test_git_rewrite_block_refuses_filter_branch() -> None:
    hooks = DeliveryHooks(instruction="Remove API keys from the repository.")
    blocked = hooks.git_rewrite_block_result(
        ToolCall(
            id="t",
            name="bash",
            arguments={"command": "git filter-branch -f --index-filter 'git rm --cached'"},
        )
    )
    assert blocked is not None
    assert blocked.success is False
    assert "original commit SHA" in (blocked.error or "")
    prune = hooks.git_rewrite_block_result(
        ToolCall(id="t", name="bash", arguments={"command": "git gc --prune=now"})
    )
    assert prune is not None
    env_prefixed = hooks.git_rewrite_block_result(
        ToolCall(
            id="t",
            name="bash",
            arguments={
                "command": (
                    "FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f "
                    "--tree-filter 'sed -i s/SECRET/x/g' HEAD"
                )
            },
        )
    )
    assert env_prefixed is not None
    dashed = hooks.git_rewrite_block_result(
        ToolCall(
            id="t",
            name="bash",
            arguments={"command": "git -C /app/dclm filter-branch -f"},
        )
    )
    assert dashed is not None
    ok = hooks.git_rewrite_block_result(
        ToolCall(id="t", name="bash", arguments={"command": "sed -i s/SECRET/x/ ray.yaml"})
    )
    assert ok is None
    hyphen = hooks.git_rewrite_block_result(
        ToolCall(
            id="t",
            name="bash",
            arguments={"command": "cd /app/dclm && git-filter-repo --invert-paths --path-glob '*.pem'"},
        )
    )
    assert hyphen is not None
    module = hooks.git_rewrite_block_result(
        ToolCall(
            id="t",
            name="bash",
            arguments={"command": "python3 -m git_filter_repo --invert-paths"},
        )
    )
    assert module is not None
    probe = hooks.git_rewrite_block_result(
        ToolCall(
            id="t",
            name="bash",
            arguments={"command": "cd /app/dclm && which git-filter-repo; git --version"},
        )
    )
    assert probe is None
    log = hooks.git_rewrite_block_result(
        ToolCall(id="t", name="bash", arguments={"command": "git log --all; git gc"})
    )
    assert log is None


def test_git_rewrite_block_refuses_helper_script_that_rewrites(tmp_path) -> None:
    """Inline veto is not enough: the next attempt is ``bash /tmp/rewrite.sh``."""
    hooks = DeliveryHooks(instruction="Remove API keys from the repository.")
    script = tmp_path / "rewrite.sh"
    script.write_text(
        "git filter-branch -f --tree-filter 'sed -i s/SECRET/x/g' -- --all\n",
        encoding="utf-8",
    )
    blocked = hooks.git_rewrite_block_result(
        ToolCall(id="t", name="bash", arguments={"command": f"bash {script}"})
    )
    assert blocked is not None
    env_prefixed = hooks.git_rewrite_block_result(
        ToolCall(
            id="t",
            name="bash",
            arguments={"command": f"FILTER=1 bash {script}"},
        )
    )
    assert env_prefixed is not None
    py = tmp_path / "rewrite.py"
    py.write_text(
        "import os\nos.system('git filter-branch -f')\n",
        encoding="utf-8",
    )
    assert (
        hooks.git_rewrite_block_result(
            ToolCall(id="t", name="bash", arguments={"command": f"python3 {py}"})
        )
        is not None
    )
    probe = tmp_path / "probe.sh"
    probe.write_text("which git-filter-repo\ngit --version\n", encoding="utf-8")
    assert (
        hooks.git_rewrite_block_result(
            ToolCall(id="t", name="bash", arguments={"command": f"bash {probe}"})
        )
        is None
    )
    sed = tmp_path / "scrub.sh"
    sed.write_text("sed -i s/SECRET/x/ ray.yaml\n", encoding="utf-8")
    assert (
        hooks.git_rewrite_block_result(
            ToolCall(id="t", name="bash", arguments={"command": f"bash {sed}"})
        )
        is None
    )


def test_git_rewrite_block_fires_on_sanitize_instruction() -> None:
    """Official TB 2.1 instruction names no history rewrite (harbor-framework/terminal-bench-2-1)."""
    instruction = (
        'Please help sanitize my github repository "dclm" of all API keys.\n'
        "Please ensure that the sensitive values are not present in the "
        "repository after the sanitization. The placeholder values should be "
        "consistent across the repository and should be kept. Also, be "
        "careful to not delete or modify any files that are not contaminated "
        "with sensitive information."
    )
    hooks = DeliveryHooks(instruction=instruction)
    blocked = hooks.git_rewrite_block_result(
        ToolCall(id="t", name="bash", arguments={"command": "git filter-branch -f"})
    )
    assert blocked is not None
    assert blocked.success is False
    hooks = DeliveryHooks(
        instruction="Rewrite history with git filter-branch to drop the secret commit."
    )
    assert (
        hooks.git_rewrite_block_result(
            ToolCall(id="t", name="bash", arguments={"command": "git filter-branch -f"})
        )
        is None
    )


@pytest.mark.asyncio
async def test_gated_executor_blocks_filter_branch_before_inner() -> None:
    ran: list[str] = []

    class _Inner:
        async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
            ran.append(call.name)
            return ToolResult(success=True, data={"ran": True})

    hooks = DeliveryHooks(instruction="Remove API keys from the repository.")
    gated = DeliveryGatedExecutor(_Inner(), hooks)
    result = await gated.execute(
        ToolCall(id="t", name="bash", arguments={"command": "git filter-branch -f"}),
        LoopContext(),
    )
    assert result.success is False
    assert ran == []


def _bmp_header(width: int, height: int) -> bytes:
    import struct

    raw = bytearray(54)
    raw[0:2] = b"BM"
    struct.pack_into("<I", raw, 10, 54)
    struct.pack_into("<I", raw, 14, 40)
    struct.pack_into("<ii", raw, 18, width, height)
    struct.pack_into("<HH", raw, 26, 1, 24)
    return bytes(raw)


@pytest.mark.asyncio
async def test_named_bmp_retries_when_header_disagrees_with_source(tmp_path) -> None:
    frame = tmp_path / "frame.bmp"
    (tmp_path / "doomgeneric_img.c").write_text("void DG_DrawFrame(void) {}\n")
    (tmp_path / "doomgeneric.h").write_text(
        "#ifndef DOOMGENERIC_RESX\n"
        "#define DOOMGENERIC_RESX 640\n"
        "#endif\n"
        "#ifndef DOOMGENERIC_RESY\n"
        "#define DOOMGENERIC_RESY 400\n"
        "#endif\n"
    )
    hooks = DeliveryHooks(
        instruction=(
            "I wrote doomgeneric_img.c which writes each frame to "
            f"{frame}."
        ),
        named_outputs=(str(frame),),
    )
    frame.write_bytes(_bmp_header(1024, 768))
    action = await hooks.before_completion(_draft(tools=4), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_image_size"
    assert "1024x768" in (action.message or "")
    assert "640x400" in (action.message or "")
    frame.write_bytes(_bmp_header(640, 400))
    done = await hooks.before_completion(_draft(tools=5), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_named_bmp_unreadable_header_retries(tmp_path) -> None:
    frame = tmp_path / "frame.bmp"
    (tmp_path / "doomgeneric_img.c").write_text(
        "#define DOOMGENERIC_RESX 640\n#define DOOMGENERIC_RESY 400\n"
    )
    hooks = DeliveryHooks(
        instruction=f"use doomgeneric_img.c to write {frame}",
        named_outputs=(str(frame),),
    )
    frame.write_bytes(b"not a bitmap")
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_image_unreadable"


@pytest.mark.asyncio
async def test_named_bmp_skips_size_check_without_source_defines(tmp_path) -> None:
    frame = tmp_path / "frame.bmp"
    hooks = DeliveryHooks(
        instruction=f"write a screenshot to {frame}",
        named_outputs=(str(frame),),
    )
    frame.write_bytes(_bmp_header(1024, 768))
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "accept"


@pytest.mark.asyncio
async def test_named_source_retries_when_over_instruction_bytes_cap(tmp_path) -> None:
    src = tmp_path / "gpt2.c"
    hooks = DeliveryHooks(
        instruction=(
            f"Write me a dependency-free C file. Call your program {src}. "
            "Your c program must be <5000 bytes."
        ),
        named_outputs=(str(src),),
    )
    src.write_bytes(b"x" * 6000)
    action = await hooks.before_completion(_draft(tools=4), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_bytes_cap"
    assert "6000" in (action.message or "")
    assert "5000" in (action.message or "")
    src.write_bytes(b"x" * 4999)
    done = await hooks.before_completion(_draft(tools=5), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_named_source_skips_bytes_cap_without_instruction_limit(
    tmp_path,
) -> None:
    src = tmp_path / "mystery.c"
    hooks = DeliveryHooks(
        instruction=f"Write {src} so it compiles with gcc.",
        named_outputs=(str(src),),
    )
    src.write_bytes(b"x" * 8000)
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "accept"


@pytest.mark.asyncio
async def test_named_source_retries_when_over_gzip_k_cap(tmp_path) -> None:
    src = tmp_path / "mystery.c"
    hooks = DeliveryHooks(
        instruction=(
            f"Write a C program {src} that performs an identical operation. "
            "Your c program must be <2k when compressed "
            "(`cat mystery.c | gzip | wc`)."
        ),
        named_outputs=(str(src),),
    )
    src.write_bytes(os.urandom(8000))
    action = await hooks.before_completion(_draft(tools=4), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_gzip_cap"
    assert "2000" in (action.message or "")
    src.write_text("int main(){return 0;}\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=5), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_named_source_skips_gzip_cap_without_gzip_pipeline(
    tmp_path,
) -> None:
    src = tmp_path / "mystery.c"
    hooks = DeliveryHooks(
        instruction=f"Write {src}. Keep it <2k.",
        named_outputs=(str(src),),
    )
    src.write_text("int x = 1;\n" * 400, encoding="utf-8")
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "accept"


@pytest.mark.asyncio
async def test_named_file_retries_when_over_instruction_mb_cap(tmp_path) -> None:
    model = tmp_path / "model.bin"
    hooks = DeliveryHooks(
        instruction=(
            f"Train a fasttext model saved as {model}. "
            "The final model size needs to be less than 1MB "
            "but get at least 0.62 accuracy."
        ),
        named_outputs=(str(model),),
    )
    model.write_bytes(b"x" * (1024 * 1024))
    action = await hooks.before_completion(_draft(tools=4), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_mb_cap"
    assert "1048576" in (action.message or "")
    model.write_bytes(b"x" * (1024 * 1024 - 1))
    done = await hooks.before_completion(_draft(tools=5), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_named_file_skips_mb_cap_without_instruction_limit(tmp_path) -> None:
    model = tmp_path / "model.bin"
    hooks = DeliveryHooks(
        instruction=f"Save the model as {model}.",
        named_outputs=(str(model),),
    )
    model.write_bytes(b"x" * (2 * 1024 * 1024))
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "accept"


@pytest.mark.asyncio
async def test_named_txt_retries_when_shown_text_is_a_raster(tmp_path) -> None:
    dest = tmp_path / "out.txt"
    hooks = DeliveryHooks(
        instruction=(
            "When I run the print, what will the text show? "
            f"Write the output to {dest}."
        ),
        named_outputs=(str(dest),),
    )
    dest.write_bytes(b"#" * 5000)
    action = await hooks.before_completion(_draft(tools=4), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_shown_text"
    dest.write_text("HELLO\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=5), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_named_txt_keeps_refusing_a_raster_after_four_retries(tmp_path) -> None:
    dest = tmp_path / "out.txt"
    hooks = DeliveryHooks(
        instruction=(
            "When I run the print, what will the text show? "
            f"Write the output to {dest}."
        ),
        named_outputs=(str(dest),),
    )
    dest.write_bytes(b"." * 8000)
    for _ in range(5):
        action = await hooks.before_completion(_draft(tools=4), LoopContext())
        assert action.kind == "retry"
        assert action.reason == "named_shown_text"
    dest.write_text("flag{short}\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=9), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_named_txt_retries_ascii_art_under_size_cap(tmp_path) -> None:
    dest = tmp_path / "out.txt"
    hooks = DeliveryHooks(
        instruction=(
            "When I run the print, what will the text show? "
            f"Write the output to {dest}."
        ),
        named_outputs=(str(dest),),
    )
    dest.write_text(("." * 40 + "\n") * 8, encoding="utf-8")
    action = await hooks.before_completion(_draft(tools=4), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_shown_text"
    dest.write_text("HELLO\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=5), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_named_txt_retries_captioned_dot_dump(tmp_path) -> None:
    """Catalog 69 shipped a labelled XY raster; art-row count catches it
    without matching the caption."""
    dest = tmp_path / "out.txt"
    hooks = DeliveryHooks(
        instruction=(
            "When I run the print, what will the text show? "
            f"Write the output to {dest}."
        ),
        named_outputs=(str(dest),),
    )
    dest.write_text(
        "=== VIEW (XY) of object at z=0.48 ===\n" + ("." * 80 + "\n") * 10,
        encoding="utf-8",
    )
    action = await hooks.before_completion(_draft(tools=4), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_shown_text"
    dest.write_text("HELLO\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=5), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_named_txt_retries_when_shown_text_is_a_stub(tmp_path) -> None:
    dest = tmp_path / "out.txt"
    hooks = DeliveryHooks(
        instruction=(
            "When I run the print, what will the text show? "
            f"Write the output to {dest}."
        ),
        named_outputs=(str(dest),),
    )
    dest.write_text("PROVISIONAL\n", encoding="utf-8")
    action = await hooks.before_completion(_draft(tools=4), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_stub"
    assert "stub" in (action.message or "")
    dest.write_text("HELLO\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=5), LoopContext())
    assert done.kind == "accept"


_CODE_FROM_IMAGE = (
    "You're given an image of a pseudocode snippet at /app/code.png. "
    "Implement the intended logic of that snippet in any language and "
    "produce the same final value it would print. Write the result to "
    "{dest}. For helping you make sure your solution is correct, the "
    "correct answer starts with `bee26a`"
)


@pytest.mark.asyncio
async def test_named_txt_retries_stub_without_shown_text_phrase(tmp_path) -> None:
    dest = tmp_path / "output.txt"
    hooks = DeliveryHooks(
        instruction=_CODE_FROM_IMAGE.format(dest=dest),
        named_outputs=(str(dest),),
    )
    dest.write_text("PLACEHOLDER\n", encoding="utf-8")
    action = await hooks.before_completion(_draft(tools=4), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_stub"


@pytest.mark.asyncio
async def test_named_txt_keeps_refusing_a_stub_after_four_retries(tmp_path) -> None:
    dest = tmp_path / "output.txt"
    hooks = DeliveryHooks(
        instruction=_CODE_FROM_IMAGE.format(dest=dest),
        named_outputs=(str(dest),),
    )
    dest.write_text("PLACEHOLDER\n", encoding="utf-8")
    for _ in range(5):
        action = await hooks.before_completion(_draft(tools=4), LoopContext())
        assert action.kind == "retry"
        assert action.reason == "named_stub"
    dest.write_text("bee26a0ff\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=9), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_named_txt_retries_when_missing_stated_prefix(tmp_path) -> None:
    dest = tmp_path / "output.txt"
    hooks = DeliveryHooks(
        instruction=_CODE_FROM_IMAGE.format(dest=dest),
        named_outputs=(str(dest),),
    )
    dest.write_text("deadbeef\n", encoding="utf-8")
    action = await hooks.before_completion(_draft(tools=4), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_prefix"
    assert "bee26a" in (action.message or "")
    dest.write_text("bee26a0ff\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=5), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_named_txt_keeps_refusing_a_hash_missing_stated_prefix(tmp_path) -> None:
    """Catalog 70 shipped a 64-hex digest that did not start with the hint."""
    dest = tmp_path / "output.txt"
    hooks = DeliveryHooks(
        instruction=_CODE_FROM_IMAGE.format(dest=dest),
        named_outputs=(str(dest),),
    )
    dest.write_text(
        "39ad5ff920409f58e961498c997af185ff1b68241c83bcac8ce67a30c05d28c5\n",
        encoding="utf-8",
    )
    for _ in range(5):
        action = await hooks.before_completion(_draft(tools=4), LoopContext())
        assert action.kind == "retry"
        assert action.reason == "named_prefix"
    dest.write_text("bee26a0ff\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=9), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_named_txt_retries_when_body_is_only_the_stated_prefix(tmp_path) -> None:
    dest = tmp_path / "output.txt"
    hooks = DeliveryHooks(
        instruction=_CODE_FROM_IMAGE.format(dest=dest),
        named_outputs=(str(dest),),
    )
    dest.write_text("bee26a\n", encoding="utf-8")
    for _ in range(5):
        action = await hooks.before_completion(_draft(tools=4), LoopContext())
        assert action.kind == "retry"
        assert action.reason == "named_prefix"
        assert "prefix" in (action.message or "").lower()
    dest.write_text("bee26a0ff\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=9), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_wrap_up_named_prefix_when_hash_misses_stated_prefix(tmp_path) -> None:
    """Catalog 70 wrote a digest then [hard_timeout]; completion retry never ran."""
    dest = tmp_path / "output.txt"
    dest.write_text(
        "39ad5ff920409f58e961498c997af185ff1b68241c83bcac8ce67a30c05d28c5\n",
        encoding="utf-8",
    )
    image = tmp_path / "code.png"
    image.write_bytes(b"\x89PNG")
    hooks = DeliveryHooks(
        instruction=_CODE_FROM_IMAGE.format(dest=dest),
        named_outputs=(str(dest),),
    )
    notice = [
        LLMMessage.text_of(
            "user",
            "[system notice] The time budget for this task is nearly exhausted.",
        )
    ]
    assert hooks.wrap_up_may_drop_tools() is False
    first = await hooks.pre_step(notice, LoopContext())
    assert first.reason == "wrap_up_named_prefix"
    assert first.tool_choice == "required"
    text = first.appends[0].message.content_text or ""
    assert "39ad5ff9" not in text
    assert "bee26a" in text
    inspect = ToolCall(id="t", name="bash", arguments={"command": f"cat {dest}"})
    assert hooks.inspect_block_result(inspect) is not None
    image_read = ToolCall(
        id="t", name="bash", arguments={"command": f"cat {image}"}
    )
    assert hooks.inspect_block_result(image_read) is None
    write = ToolCall(
        id="t",
        name="bash",
        arguments={"command": f"cat > {dest} <<'EOF'\nbee26a0ff\nEOF"},
    )
    assert hooks.inspect_block_result(write) is None
    dest.write_text("bee26a0ff\n", encoding="utf-8")
    assert hooks.wrap_up_may_drop_tools() is True


@pytest.mark.asyncio
async def test_wrap_up_prefix_keeps_required_after_a_tool(tmp_path) -> None:
    """Catalog 70 already wrote a digest; any later tool used to clear
    ``_force_tool`` and leave wrap-up without ``tool_choice=required``."""
    dest = tmp_path / "output.txt"
    hooks = DeliveryHooks(
        instruction=_CODE_FROM_IMAGE.format(dest=dest),
        named_outputs=(str(dest),),
    )
    dest.write_text(
        "39ad5ff920409f58e961498c997af185ff1b68241c83bcac8ce67a30c05d28c5\n",
        encoding="utf-8",
    )
    notice = [
        LLMMessage.text_of(
            "user",
            "[system notice] The time budget for this task is nearly exhausted.",
        )
    ]
    ctx = LoopContext()
    ok = ToolResult(success=True, data={})
    await hooks.post_tool_result(ok, _call("write_file"), ctx)
    first = await hooks.pre_step(notice, ctx)
    assert first.reason == "wrap_up_named_prefix"
    await hooks.post_tool_result(ok, _call("bash"), ctx)
    later = await hooks.pre_step(notice, ctx)
    assert later.tool_choice == "required"
    dest.write_text("bee26a0ff\n", encoding="utf-8")
    done = await hooks.pre_step(notice, ctx)
    assert done.tool_choice is None


@pytest.mark.asyncio
async def test_wrap_up_named_prefix_when_body_is_only_the_prefix(tmp_path) -> None:
    dest = tmp_path / "output.txt"
    dest.write_text("bee26a\n", encoding="utf-8")
    hooks = DeliveryHooks(
        instruction=_CODE_FROM_IMAGE.format(dest=dest),
        named_outputs=(str(dest),),
    )
    notice = [
        LLMMessage.text_of(
            "user",
            "[system notice] The time budget for this task is nearly exhausted.",
        )
    ]
    first = await hooks.pre_step(notice, LoopContext())
    assert first.reason == "wrap_up_named_prefix"
    assert "prefix" in (first.appends[0].message.content_text or "").lower()
    second = await hooks.pre_step(notice, LoopContext())
    assert second.reason != "wrap_up_named_prefix"


@pytest.mark.asyncio
async def test_named_txt_skips_prefix_check_without_starts_with(tmp_path) -> None:
    dest = tmp_path / "move.txt"
    hooks = DeliveryHooks(
        instruction=f"Write the best move to {dest} for example e2e4.",
        named_outputs=(str(dest),),
    )
    dest.write_text("e2e4\n", encoding="utf-8")
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "accept"


_CIRCUIT_EXAMPLES = (
    "For example, the provided example /app/gates.txt file prints outputs "
    "argv[1]/2.\n"
    "As an example, running {sim} 208 should output 377 because isqrt(208)=14 "
    "and fib(14) = 377,\n"
    "and running {sim} 20000 should give 1407432322."
)


def test_instruction_example_commands_parses_running_should_output() -> None:
    """Official TB 2.1 circuit-fibsqrt wording (harbor-framework/terminal-bench-2-1)."""
    text = (
        "For example, the provided example /app/gates.txt file prints outputs "
        "argv[1]/2.\n"
        "As an example, running /app/sim 208 should output 377 because "
        "isqrt(208)=14 and fib(14) = 377,\n"
        "and running /app/sim 20000 should give 1407432322."
    )
    assert instruction_example_commands(text) == (
        (("/app/sim", "208"), "377"),
        (("/app/sim", "20000"), "1407432322"),
    )
    assert instruction_example_commands(
        'This should output "Results: X Y Z" where X, Y, Z are integers.'
    ) == ()
    assert instruction_example_commands(
        "The .DAT files produced by running /app/program.py must be identical."
    ) == ()
    assert instruction_example_commands(
        "If there are multiple winning moves, print them all, one per line."
    ) == ()


def _example_sim(path: Path, script: str) -> Path:
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.asyncio
async def test_instruction_example_retries_when_stdout_mismatches() -> None:
    sim = _example_sim(
        Path("/tmp") / "steerable-example-sim-mismatch",
        "#!/bin/sh\necho 104\n",
    )
    try:
        dest = Path("/tmp/steerable-example-gates.txt")
        dest.write_text("out0 = 0\n", encoding="utf-8")
        hooks = DeliveryHooks(
            instruction=_CIRCUIT_EXAMPLES.format(sim=sim),
            named_outputs=(str(dest),),
        )
        action = await hooks.before_completion(_draft(tools=4), LoopContext())
        assert action.kind == "retry"
        assert action.reason == "instruction_example"
        assert "377" in (action.message or "")
        assert "104" in (action.message or "")
    finally:
        sim.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_instruction_example_keeps_refusing_a_mismatch() -> None:
    sim = _example_sim(
        Path("/tmp") / "steerable-example-sim-hold",
        "#!/bin/sh\necho 104\n",
    )
    dest = Path("/tmp/steerable-example-gates-hold.txt")
    dest.write_text("out0 = 0\n", encoding="utf-8")
    try:
        hooks = DeliveryHooks(
            instruction=_CIRCUIT_EXAMPLES.format(sim=sim),
            named_outputs=(str(dest),),
        )
        for _ in range(5):
            action = await hooks.before_completion(_draft(tools=4), LoopContext())
            assert action.kind == "retry"
            assert action.reason == "instruction_example"
        sim.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = 208 ]; then echo 377; else echo 1407432322; fi\n",
            encoding="utf-8",
        )
        sim.chmod(0o755)
        done = await hooks.before_completion(_draft(tools=9), LoopContext())
        assert done.kind == "accept"
    finally:
        sim.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_instruction_example_runs_with_binary_parent_cwd() -> None:
    """Official sim.c reads gates.txt from cwd; hidden tests invoke /app/sim."""
    root = Path("/tmp/steerable-example-cwd")
    root.mkdir(parents=True, exist_ok=True)
    sim = _example_sim(root / "sim", "#!/bin/sh\ncat gates.txt\n")
    (root / "gates.txt").write_text("377\n", encoding="utf-8")
    dest = Path("/tmp/steerable-example-cwd-named.txt")
    dest.write_text("x\n", encoding="utf-8")
    try:
        hooks = DeliveryHooks(
            instruction=f"As an example, running {sim} 208 should output 377.",
            named_outputs=(str(dest),),
        )
        action = await hooks.before_completion(_draft(tools=4), LoopContext())
        assert action.kind == "accept"
    finally:
        sim.unlink(missing_ok=True)
        (root / "gates.txt").unlink(missing_ok=True)
        dest.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_instruction_example_compiles_sibling_c_when_binary_missing() -> None:
    """TB circuit image ships sim.c; /app/sim is not in the image."""
    root = Path("/tmp/steerable-example-compile")
    root.mkdir(parents=True, exist_ok=True)
    sim = root / "sim"
    src = root / "sim.c"
    dest = Path("/tmp/steerable-example-compile-gates.txt")
    sim.unlink(missing_ok=True)
    src.write_text(
        '#include <stdio.h>\nint main(void){puts("104");return 0;}\n',
        encoding="utf-8",
    )
    dest.write_text("out0 = 0\n", encoding="utf-8")
    try:
        hooks = DeliveryHooks(
            instruction=_CIRCUIT_EXAMPLES.format(sim=sim),
            named_outputs=(str(dest),),
        )
        action = await hooks.before_completion(_draft(tools=4), LoopContext())
        assert action.kind == "retry"
        assert action.reason == "instruction_example"
        assert "104" in (action.message or "")
        assert sim.is_file()
    finally:
        sim.unlink(missing_ok=True)
        src.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_wrap_up_instruction_example_when_starter_mismatches() -> None:
    root = Path("/tmp/steerable-example-wrap")
    root.mkdir(parents=True, exist_ok=True)
    sim = root / "sim"
    src = root / "sim.c"
    dest = Path("/tmp/steerable-example-wrap-gates.txt")
    sim.unlink(missing_ok=True)
    src.write_text(
        '#include <stdio.h>\nint main(void){puts("104");return 0;}\n',
        encoding="utf-8",
    )
    dest.write_text("out0 = 0\n", encoding="utf-8")
    notice = [
        LLMMessage.text_of(
            "user",
            "[system notice] The time budget for this task is nearly exhausted.",
        )
    ]
    try:
        hooks = DeliveryHooks(
            instruction=_CIRCUIT_EXAMPLES.format(sim=sim),
            named_outputs=(str(dest),),
        )
        assert hooks.wrap_up_may_drop_tools() is False
        first = await hooks.pre_step(notice, LoopContext())
        assert first.reason == "wrap_up_instruction_example"
        assert first.tool_choice == "required"
        assert "104" in (first.appends[0].message.content_text or "")
        assert hooks.wrap_up_may_drop_tools() is False
        ctx = LoopContext()
        ok = ToolResult(success=True, data={})
        await hooks.post_tool_result(ok, _call("write_file"), ctx)
        later = await hooks.pre_step(notice, ctx)
        assert later.tool_choice == "required"
        assert later.reason != "wrap_up_instruction_example"
        inspect = ToolCall(id="t", name="bash", arguments={"command": f"cat {dest}"})
        blocked = hooks.inspect_block_result(inspect)
        assert blocked is not None
        write = ToolCall(
            id="t",
            name="bash",
            arguments={"command": f"cat > {dest} <<'EOF'\nfixed\nEOF"},
        )
        assert hooks.inspect_block_result(write) is None
        helper = dest.parent / "gen.py"
        helper.write_text(
            "from pathlib import Path\n"
            f"Path({dest.name!r}).write_text('fixed\\n')\n",
            encoding="utf-8",
        )
        generate = ToolCall(
            id="t",
            name="bash",
            arguments={"command": f"python3 {helper}"},
        )
        assert hooks.inspect_block_result(generate) is None
        look = dest.parent / "look.py"
        look.write_text(f"print(open({dest.name!r}).read())\n", encoding="utf-8")
        inspect_py = ToolCall(
            id="t",
            name="bash",
            arguments={"command": f"python3 {look}"},
        )
        assert hooks.inspect_block_result(inspect_py) is not None
        helper.unlink(missing_ok=True)
        look.unlink(missing_ok=True)
    finally:
        sim.unlink(missing_ok=True)
        src.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_named_json_retries_when_string_has_newline(tmp_path) -> None:
    dest = tmp_path / "re.json"
    hooks = DeliveryHooks(
        instruction=(
            "Write /app/re.json. "
            "return fen.split(\"\\n\")"
        ),
        named_outputs=(str(dest),),
    )
    dest.write_text('[["a", "b\\n"]]', encoding="utf-8")
    action = await hooks.before_completion(_draft(tools=4), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_json_blank"
    dest.write_text('[["a", "b"]]', encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=5), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_named_json_retries_when_replacement_is_empty(tmp_path) -> None:
    dest = tmp_path / "re.json"
    hooks = DeliveryHooks(
        instruction='pairs; fen.split("\\n")',
        named_outputs=(str(dest),),
    )
    dest.write_text('[["a", ""]]', encoding="utf-8")
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_json_blank"


@pytest.mark.asyncio
async def test_named_json_skips_blank_check_without_split(tmp_path) -> None:
    dest = tmp_path / "data.json"
    hooks = DeliveryHooks(
        instruction=f"write {dest}",
        named_outputs=(str(dest),),
    )
    dest.write_text('[["a", "b\\n"]]', encoding="utf-8")
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "accept"


@pytest.mark.asyncio
async def test_named_checker_accepts_when_script_exits_zero(tmp_path) -> None:
    dest = tmp_path / "re.json"
    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")
    hooks = DeliveryHooks(
        instruction=(
            f"Write {dest}. You can look at the provided check.py "
            "to verify if your solution is correct."
        ),
        named_outputs=(str(dest),),
    )
    dest.write_text('[["a", "b"]]', encoding="utf-8")
    action = await hooks.before_completion(_draft(tools=4), LoopContext())
    assert action.kind == "accept"


@pytest.mark.asyncio
async def test_named_checker_retries_when_script_exits_nonzero(tmp_path) -> None:
    dest = tmp_path / "re.json"
    (tmp_path / "check.py").write_text(
        "import sys\nprint('Our move:  ')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    hooks = DeliveryHooks(
        instruction=(
            f"Write {dest}. You can look at the provided check.py "
            "to verify if your solution is correct."
        ),
        named_outputs=(str(dest),),
    )
    dest.write_text('[["a", "b"]]', encoding="utf-8")
    action = await hooks.before_completion(_draft(tools=4), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_checker"
    assert "check.py" in (action.message or "")
    assert "Our move:" in (action.message or "")
    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=5), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_named_checker_retries_when_eval_py_fails(tmp_path) -> None:
    dest = tmp_path / "eigen.py"
    (tmp_path / "eval.py").write_text(
        "import sys\nsys.exit(2)\n",
        encoding="utf-8",
    )
    hooks = DeliveryHooks(
        instruction=(
            f"Complete {dest}. `/app/eval.py` can help you iterate."
        ),
        named_outputs=(str(dest),),
    )
    dest.write_text("def find():\n    return 1\n", encoding="utf-8")
    action = await hooks.before_completion(_draft(tools=4), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_checker"
    assert "eval.py" in (action.message or "")
    assert "exited 2" in (action.message or "")


@pytest.mark.asyncio
async def test_named_checker_stops_after_two_failures(tmp_path) -> None:
    dest = tmp_path / "re.json"
    (tmp_path / "check.py").write_text(
        "import sys\nsys.exit(1)\n",
        encoding="utf-8",
    )
    hooks = DeliveryHooks(
        instruction=f"Write {dest}. Use check.py.",
        named_outputs=(str(dest),),
    )
    dest.write_text("[]\n", encoding="utf-8")
    first = await hooks.before_completion(_draft(tools=4), LoopContext())
    assert first.kind == "retry"
    second = await hooks.before_completion(_draft(tools=5), LoopContext())
    assert second.kind == "retry"
    third = await hooks.before_completion(_draft(tools=6), LoopContext())
    assert third.kind == "accept"


@pytest.mark.asyncio
async def test_named_checker_skips_when_outputs_still_missing(tmp_path) -> None:
    dest = tmp_path / "re.json"
    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")
    hooks = DeliveryHooks(
        instruction=f"Write {dest}. Use check.py.",
        named_outputs=(str(dest),),
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "missing_named_output"


@pytest.mark.asyncio
async def test_named_empty_file_retries(tmp_path) -> None:
    dest = tmp_path / "out.txt"
    hooks = DeliveryHooks(
        instruction=f"Write {dest}",
        named_outputs=(str(dest),),
    )
    dest.write_text("", encoding="utf-8")
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_empty"
    dest.write_text("payload\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=3), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_named_utf8_source_retries(tmp_path) -> None:
    dest = tmp_path / "prog.c"
    hooks = DeliveryHooks(
        instruction=f"Write {dest} under 5000 bytes",
        named_outputs=(str(dest),),
    )
    dest.write_bytes(b"\xff\xfeint main(void) { return 0; }\n")
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_utf8"
    dest.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=3), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_named_python_syntax_retries(tmp_path) -> None:
    dest = tmp_path / "solve.py"
    hooks = DeliveryHooks(
        instruction=f"Write {dest}",
        named_outputs=(str(dest),),
    )
    dest.write_text("def (\n", encoding="utf-8")
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_syntax"
    dest.write_text("def main():\n    return 0\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=3), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_workspace_check_py_runs_without_being_named(tmp_path) -> None:
    dest = tmp_path / "out.txt"
    dest.write_text("stale\n", encoding="utf-8")
    (tmp_path / "check.py").write_text(
        "import sys\nprint('mismatch')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    hooks = DeliveryHooks(
        instruction=f"Edit {dest} in place.",
        named_outputs=(str(dest),),
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_checker"
    assert "check.py" in (action.message or "")
    assert "mismatch" in (action.message or "")
    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=3), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_eval_py_not_run_unless_instruction_names_it(tmp_path) -> None:
    dest = tmp_path / "out.txt"
    dest.write_text("ok\n", encoding="utf-8")
    (tmp_path / "eval.py").write_text(
        "import sys\nsys.exit(1)\n",
        encoding="utf-8",
    )
    hooks = DeliveryHooks(
        instruction=f"Write {dest}",
        named_outputs=(str(dest),),
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "accept"


@pytest.mark.asyncio
async def test_does_not_run_required_check_py_as_checker(tmp_path) -> None:
    dest = tmp_path / "check.py"
    hooks = DeliveryHooks(
        instruction=f"Write {dest}",
        named_outputs=(str(dest),),
    )
    dest.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "accept"


@pytest.mark.asyncio
async def test_named_entrypoint_writes_missing_output(tmp_path) -> None:
    dest = tmp_path / "out.txt"
    script = tmp_path / "helper.py"
    script.write_text(
        f"from pathlib import Path\nPath({str(dest)!r}).write_text('ok\\n')\n",
        encoding="utf-8",
    )
    hooks = DeliveryHooks(
        instruction=f"Write {dest} by running `python3 helper.py`.",
        named_outputs=(str(dest),),
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert dest.read_text(encoding="utf-8") == "ok\n"
    assert action.kind == "accept"


@pytest.mark.asyncio
async def test_named_entrypoint_retries_when_command_fails(tmp_path) -> None:
    dest = tmp_path / "out.txt"
    script = tmp_path / "helper.py"
    script.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    hooks = DeliveryHooks(
        instruction=f"Write {dest} by running `python3 helper.py`.",
        named_outputs=(str(dest),),
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_entrypoint"
    assert "exited 3" in (action.message or "")
    assert not dest.exists()


@pytest.mark.asyncio
async def test_named_entrypoint_skips_when_named_input_missing(tmp_path) -> None:
    frame = tmp_path / "frame.bmp"
    elf = tmp_path / "doomgeneric_mips"
    script = tmp_path / "vm.js"
    script.write_text("throw new Error('should not run')\n", encoding="utf-8")
    hooks = DeliveryHooks(
        instruction=(
            f"ELF called doomgeneric_mips, run `node vm.js`, "
            f"frames written to {frame}"
        ),
        named_outputs=(str(elf), str(frame)),
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "missing_named_output"
    assert hooks._entry_runs == 0


@pytest.mark.asyncio
async def test_named_entrypoint_runs_when_only_side_effect_missing(
    tmp_path,
) -> None:
    frame = tmp_path / "out.txt"
    elf = tmp_path / "doomgeneric_mips"
    elf.write_bytes(b"\x7fELF")
    script = tmp_path / "helper.py"
    script.write_text(
        f"from pathlib import Path\nPath({str(frame)!r}).write_text('ok\\n')\n",
        encoding="utf-8",
    )
    hooks = DeliveryHooks(
        instruction=(
            f"ELF called doomgeneric_mips, run `python3 helper.py`, "
            f"write {frame}"
        ),
        named_outputs=(str(elf), str(frame)),
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert frame.read_text(encoding="utf-8") == "ok\n"
    assert action.kind == "accept"
    assert hooks._entry_runs == 1


@pytest.mark.asyncio
async def test_named_script_runs_without_backticks_when_side_effect_missing(
    tmp_path,
) -> None:
    npy = tmp_path / "weights.npy"
    script = tmp_path / "helper.py"
    hooks = DeliveryHooks(
        instruction=(
            f"write a file called helper.py that when run saves {npy}"
        ),
        named_outputs=(str(script), str(npy)),
    )
    script.write_text(
        f"from pathlib import Path\nPath({str(npy)!r}).write_bytes(b'x')\n",
        encoding="utf-8",
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert npy.read_bytes() == b"x"
    assert action.kind == "accept"
    assert hooks._entry_runs == 1


@pytest.mark.asyncio
async def test_named_make_builds_missing_elf(tmp_path) -> None:
    elf = tmp_path / "doomgeneric_mips"
    (tmp_path / "Makefile").write_text(
        f"all:\n\tprintf x > {elf.name}\n",
        encoding="utf-8",
    )
    hooks = DeliveryHooks(
        instruction="producing an ELF called doomgeneric_mips",
        named_outputs=(str(elf),),
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert elf.is_file()
    assert elf.read_text(encoding="utf-8") == "x"
    assert action.kind == "accept"
    assert hooks._make_runs == 1


@pytest.mark.asyncio
async def test_named_make_then_entrypoint_fills_side_effect(tmp_path) -> None:
    elf = tmp_path / "doomgeneric_mips"
    frame = tmp_path / "frame.bmp"
    (tmp_path / "Makefile").write_text(
        f"all:\n\tprintf x > {elf.name}\n",
        encoding="utf-8",
    )
    (tmp_path / "helper.py").write_text(
        f"from pathlib import Path\nPath({str(frame)!r}).write_text('ok\\n')\n",
        encoding="utf-8",
    )
    hooks = DeliveryHooks(
        instruction=(
            f"ELF called doomgeneric_mips, run `python3 helper.py`, "
            f"write {frame}"
        ),
        named_outputs=(str(elf), str(frame)),
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert elf.is_file()
    assert frame.read_text(encoding="utf-8") == "ok\n"
    assert action.kind == "accept"
    assert hooks._make_runs == 1
    assert hooks._entry_runs == 1


@pytest.mark.asyncio
async def test_named_make_retries_when_make_fails(tmp_path) -> None:
    elf = tmp_path / "doomgeneric_mips"
    (tmp_path / "Makefile").write_text("all:\n\tfalse\n", encoding="utf-8")
    hooks = DeliveryHooks(
        instruction="producing an ELF called doomgeneric_mips",
        named_outputs=(str(elf),),
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "named_make"
    assert not elf.exists()


@pytest.mark.asyncio
async def test_named_make_skips_when_only_side_effect_missing(tmp_path) -> None:
    dest = tmp_path / "out.txt"
    (tmp_path / "Makefile").write_text("all:\n\tfalse\n", encoding="utf-8")
    hooks = DeliveryHooks(
        instruction=f"Write {dest}",
        named_outputs=(str(dest),),
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.reason == "missing_named_output"
    assert hooks._make_runs == 0


@pytest.mark.asyncio
async def test_named_directory_does_not_rerun_make(tmp_path) -> None:
    dest = tmp_path / "povray-2.2"
    (tmp_path / "Makefile").write_text("all:\n\tfalse\n", encoding="utf-8")
    hooks = DeliveryHooks(
        instruction=f"compile {dest}",
        named_outputs=(str(dest),),
    )
    dest.mkdir()
    (dest / "src.c").write_text("int main(){}\n", encoding="utf-8")
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "accept"
    assert hooks._make_runs == 0


@pytest.mark.asyncio
async def test_named_make_skips_missing_js_source(tmp_path) -> None:
    elf = tmp_path / "doomgeneric_mips"
    elf.write_bytes(b"\x7fELF")
    vm = tmp_path / "vm.js"
    frame = tmp_path / "frame.bmp"
    (tmp_path / "Makefile").write_text("all:\n\tfalse\n", encoding="utf-8")
    hooks = DeliveryHooks(
        instruction=(
            "interpreter called vm.js so I can run `node vm.js` "
            f"on the ELF called doomgeneric_mips; frames written to {frame}"
        ),
        named_outputs=(str(vm), str(elf), str(frame)),
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.reason == "missing_named_output"
    assert hooks._make_runs == 0
    assert hooks._entry_runs == 0
    assert not vm.exists()


@pytest.mark.asyncio
async def test_named_make_finds_makefile_two_levels_down(tmp_path) -> None:
    elf = tmp_path / "doomgeneric_mips"
    nested = tmp_path / "doomgeneric" / "doomgeneric"
    nested.mkdir(parents=True)
    (nested / "Makefile").write_text(
        f"all:\n\tprintf x > {elf}\n",
        encoding="utf-8",
    )
    hooks = DeliveryHooks(
        instruction="producing an ELF called doomgeneric_mips",
        named_outputs=(str(elf),),
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert elf.is_file()
    assert elf.read_text(encoding="utf-8") == "x"
    assert action.kind == "accept"
    assert hooks._make_runs == 1


@pytest.mark.asyncio
async def test_named_make_promotes_basename_next_to_makefile(tmp_path) -> None:
    dest = tmp_path / "doomgeneric_mips"
    nested = tmp_path / "doomgeneric" / "doomgeneric"
    nested.mkdir(parents=True)
    (nested / "Makefile").write_text(
        "all:\n\tprintf x > doomgeneric_mips\n",
        encoding="utf-8",
    )
    hooks = DeliveryHooks(
        instruction="producing an ELF called doomgeneric_mips",
        named_outputs=(str(dest),),
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert dest.is_file()
    assert dest.read_text(encoding="utf-8") == "x"
    assert action.kind == "accept"
    assert hooks._make_runs == 1


@pytest.mark.asyncio
async def test_named_gcc_entrypoint_writes_ppm(tmp_path) -> None:
    source = tmp_path / "image.c"
    ppm = tmp_path / "reconstructed.ppm"
    source.write_text(
        "#include <stdio.h>\n"
        "int main(void) {\n"
        '  FILE *f = fopen("reconstructed.ppm", "w");\n'
        '  fputs("P3\\n1 1\\n255\\n0 0 0\\n", f);\n'
        "  fclose(f);\n"
        "  return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    hooks = DeliveryHooks(
        instruction=(
            f"Write a c program image.c. Output to a new file reconstructed.ppm. "
            f"I will test it by calling gcc -o image {source.name} && ./image"
        ),
        named_outputs=(str(source), str(ppm)),
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert ppm.is_file()
    assert "P3" in ppm.read_text(encoding="utf-8")
    assert action.kind == "accept"
    assert hooks._entry_runs == 1


def test_wrap_up_may_drop_tools_while_named_missing(tmp_path) -> None:
    missing = tmp_path / "ars.R"
    hooks = DeliveryHooks(named_outputs=(str(missing),))
    assert hooks.wrap_up_may_drop_tools() is False
    missing.write_text("ars <- function() {}\n", encoding="utf-8")
    assert hooks.wrap_up_may_drop_tools() is True
    bare = DeliveryHooks(instruction="Solve the puzzle.", named_outputs=())
    assert bare.wrap_up_may_drop_tools() is True


def test_wrap_up_keeps_tools_when_seeded_named_path_vanishes(tmp_path) -> None:
    seeded = tmp_path / "seed.dat"
    seeded.write_text("", encoding="utf-8")
    hooks = DeliveryHooks(named_outputs=(str(seeded),))
    assert hooks._delivery_missing() == ()
    assert hooks.wrap_up_may_drop_tools() is True
    seeded.unlink()
    assert hooks._delivery_missing() == (str(seeded),)
    assert hooks.wrap_up_may_drop_tools() is False


def test_only_writes_count_as_progress() -> None:
    """Inspection is what a spiral does; resetting the loop's no-progress
    budget on it would keep the budget from ever accumulating."""
    hooks = DeliveryHooks(named_outputs=())
    ok = ToolResult(success=True, message="fine")

    def bash(command: str) -> ToolCall:
        return ToolCall(id="t", name="bash", arguments={"command": command})

    assert hooks.tool_made_progress(ok, _call("write_file")) is True
    assert hooks.tool_made_progress(ok, bash("cat > /app/out.py")) is True
    assert hooks.tool_made_progress(ok, bash("make solver")) is True
    assert hooks.tool_made_progress(ok, _call("read_file")) is False
    assert hooks.tool_made_progress(ok, bash("ls -la /app")) is False
    assert hooks.tool_made_progress(ok, bash("grep -rn foo .")) is False
    # A write that failed delivered nothing.
    failed = ToolResult(success=False, error="permission denied")
    assert hooks.tool_made_progress(failed, _call("write_file")) is False


@pytest.mark.asyncio
async def test_missing_named_retries_when_seeded_file_vanishes(tmp_path) -> None:
    seeded = tmp_path / "seed.dat"
    seeded.write_text("keep\n", encoding="utf-8")
    hooks = DeliveryHooks(named_outputs=(str(seeded),))
    ctx = LoopContext()
    first = await hooks.before_completion(_draft(tools=2), ctx)
    assert first.kind == "accept"
    seeded.unlink()
    second = await hooks.before_completion(_draft(tools=2), ctx)
    assert second.reason == "missing_named_output"
    assert str(seeded) in (second.message or "")


@pytest.mark.asyncio
async def test_wrap_up_named_when_seeded_file_vanishes(tmp_path) -> None:
    dest = tmp_path / "seed.dat"
    dest.write_text("ok\n", encoding="utf-8")
    hooks = DeliveryHooks(named_outputs=(str(dest),))
    notice = [
        LLMMessage.text_of(
            "user",
            "[system notice] The time budget for this task is nearly exhausted.",
        )
    ]
    first = await hooks.pre_step(notice, LoopContext())
    assert first.reason != "wrap_up_named_output"
    dest.unlink()
    second = await hooks.pre_step(notice, LoopContext())
    assert second.reason == "wrap_up_named_output"
    assert str(dest) in (second.appends[0].message.content_text or "")


def test_wrap_up_may_drop_tools_while_telnet_closed() -> None:
    port = _closed_tcp_port()
    hooks = DeliveryHooks(
        instruction=(
            f"Connect with `telnet 127.0.0.1 {port}` and wait for a login prompt."
        ),
        named_outputs=(),
    )
    assert hooks.wrap_up_may_drop_tools() is False
    hooks._listen_retries = 4
    assert hooks.wrap_up_may_drop_tools() is True


def test_wrap_up_may_drop_tools_while_monitor_socket_missing() -> None:
    sock = Path(f"/tmp/steerable-test-monitor-{os.getpid()}.sock")
    sock.unlink(missing_ok=True)
    hooks = DeliveryHooks(
        instruction=(
            f"Configure a QEMU monitor socket at `{sock}` for "
            "programmatic keyboard input."
        ),
        named_outputs=(),
    )
    assert hooks.wrap_up_may_drop_tools() is False
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(sock))
        srv.listen(1)
        assert hooks.wrap_up_may_drop_tools() is True
    finally:
        srv.close()
        sock.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_monitor_socket_retries_when_missing() -> None:
    sock = Path(f"/tmp/steerable-test-monitor-missing-{os.getpid()}.sock")
    sock.unlink(missing_ok=True)
    hooks = DeliveryHooks(
        instruction=(
            f"Configure a QEMU monitor socket at `{sock}` for "
            "programmatic keyboard input."
        ),
        named_outputs=(),
        min_tools_for_completion_retry=99,
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "instruction_socket"
    assert str(sock) in (action.message or "")


@pytest.mark.asyncio
async def test_monitor_socket_retries_when_regular_file() -> None:
    sock = Path(f"/tmp/steerable-test-monitor-file-{os.getpid()}.sock")
    sock.write_text("not a socket\n", encoding="utf-8")
    hooks = DeliveryHooks(
        instruction=(
            f"Configure a QEMU monitor socket at `{sock}` for "
            "programmatic keyboard input."
        ),
        named_outputs=(),
        min_tools_for_completion_retry=99,
    )
    try:
        action = await hooks.before_completion(_draft(tools=2), LoopContext())
        assert action.kind == "retry"
        assert action.reason == "instruction_socket"
    finally:
        sock.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_monitor_socket_accepts_when_unix_socket() -> None:
    sock = Path(f"/tmp/steerable-test-monitor-ok-{os.getpid()}.sock")
    sock.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(sock))
        srv.listen(1)
        hooks = DeliveryHooks(
            instruction=(
                f"Configure a QEMU monitor socket at `{sock}` for "
                "programmatic keyboard input."
            ),
            named_outputs=(),
            min_tools_for_completion_retry=99,
        )
        action = await hooks.before_completion(_draft(tools=2), LoopContext())
        assert action.kind == "accept"
    finally:
        srv.close()
        sock.unlink(missing_ok=True)


def test_wrap_up_may_drop_tools_when_telnet_listens() -> None:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()[:2]
    try:
        hooks = DeliveryHooks(
            instruction=(
                f"Connect with `telnet {host} {port}` and wait for a login prompt."
            ),
            named_outputs=(),
        )
        if Path("/proc/net/tcp").is_file():
            # pytest owns the socket; a script listener is not the VM serial.
            assert hooks.wrap_up_may_drop_tools() is False
        else:
            assert hooks.wrap_up_may_drop_tools() is True
    finally:
        srv.close()


def test_wrap_up_may_drop_tools_while_cpu_only_off(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "build" / "CMakeCache.txt"
    cache.parent.mkdir()
    cache.write_text("CPU_ONLY:BOOL=OFF\n", encoding="utf-8")
    hooks = DeliveryHooks(
        instruction="build for only CPU execution",
        named_outputs=(),
    )
    assert hooks.wrap_up_may_drop_tools() is False
    cache.write_text("CPU_ONLY:BOOL=ON\n", encoding="utf-8")
    assert hooks.wrap_up_may_drop_tools() is True


def _closed_tcp_port() -> int:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = int(srv.getsockname()[1])
    srv.close()
    return port


def _serve_once(payload: bytes | None) -> tuple[str, int]:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()[:2]

    def run() -> None:
        conn, _ = srv.accept()
        try:
            if payload:
                conn.sendall(payload)
        finally:
            conn.close()
            srv.close()

    threading.Thread(target=run, daemon=True).start()
    return host, port


@pytest.mark.asyncio
async def test_telnet_retries_when_nothing_listens() -> None:
    port = _closed_tcp_port()
    hooks = DeliveryHooks(
        instruction=(
            f"Connect with `telnet 127.0.0.1 {port}` and wait for a login prompt."
        ),
        named_outputs=(),
        min_tools_for_completion_retry=99,
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "instruction_listen"
    assert f"telnet 127.0.0.1 {port}" in (action.message or "")


@pytest.mark.asyncio
async def test_telnet_accepts_when_port_listens() -> None:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()[:2]
    try:
        hooks = DeliveryHooks(
            instruction=(
                f"Connect with `telnet {host} {port}` and wait for a login prompt."
            ),
            named_outputs=(),
            min_tools_for_completion_retry=99,
        )
        action = await hooks.before_completion(_draft(tools=2), LoopContext())
        if Path("/proc/net/tcp").is_file():
            assert action.kind == "retry"
            assert action.reason == "instruction_listen"
            assert "userspace" in (action.message or "")
        else:
            assert action.kind == "accept"
    finally:
        srv.close()


@pytest.mark.asyncio
async def test_telnet_accepts_when_hypervisor_owns_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "steerable_sidecar.delivery._telnet_status",
        lambda host, port: ("ready", "qemu-system-x86_64"),
    )
    hooks = DeliveryHooks(
        instruction=(
            "Connect with `telnet 127.0.0.1 6665` and wait for a login prompt."
        ),
        named_outputs=(),
        min_tools_for_completion_retry=99,
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "accept"


def test_script_listener_comm_names() -> None:
    assert _is_script_listener("python3.13")
    assert _is_script_listener("python")
    assert _is_script_listener("/usr/bin/python3")
    assert not _is_script_listener("qemu-system-x86_64")
    assert not _is_script_listener("socat")


@pytest.mark.asyncio
async def test_nginx_port_retries_when_closed() -> None:
    port = _closed_tcp_port()
    hooks = DeliveryHooks(
        instruction=(
            f"Set up a web interface (nginx) on port {port} for remote access."
        ),
        named_outputs=(),
        min_tools_for_completion_retry=99,
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "instruction_listen"
    assert f"port {port}" in (action.message or "")


@pytest.mark.asyncio
async def test_nginx_port_accepts_when_listening() -> None:
    _host, port = _serve_once(b"")
    hooks = DeliveryHooks(
        instruction=(
            f"Set up a web interface (nginx) on port {port} for remote access."
        ),
        named_outputs=(),
        min_tools_for_completion_retry=99,
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "accept"


@pytest.mark.asyncio
async def test_listen_skips_when_instruction_names_no_port() -> None:
    hooks = DeliveryHooks(
        instruction="Write a headless terminal emulator. No ports are named.",
        named_outputs=(),
        min_tools_for_completion_retry=99,
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "accept"
    assert action.reason is None


@pytest.mark.asyncio
async def test_cpu_only_retries_when_cmake_cache_is_off(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "caffe" / "build" / "CMakeCache.txt"
    cache.parent.mkdir(parents=True)
    cache.write_text("# cache\nCPU_ONLY:BOOL=OFF\n", encoding="utf-8")
    hooks = DeliveryHooks(
        instruction="Install Caffe 1.0.0 and build for only CPU execution.",
        named_outputs=(),
        min_tools_for_completion_retry=99,
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "instruction_cpu_only"
    cache.write_text("CPU_ONLY:BOOL=ON\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=3), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_cpu_only_makefile_beats_cmake_cache(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    caffe = tmp_path / "caffe"
    caffe.mkdir()
    (caffe / "Makefile.config").write_text("CPU_ONLY := 0\n", encoding="utf-8")
    cache = caffe / "build" / "CMakeCache.txt"
    cache.parent.mkdir()
    cache.write_text("CPU_ONLY:BOOL=ON\n", encoding="utf-8")
    hooks = DeliveryHooks(
        instruction="build for only CPU execution",
        named_outputs=(),
        min_tools_for_completion_retry=99,
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "instruction_cpu_only"
    (caffe / "Makefile.config").write_text("CPU_ONLY := 1\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=3), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_cpu_only_skips_when_instruction_omits_cpu(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "build" / "CMakeCache.txt"
    cache.parent.mkdir()
    cache.write_text("CPU_ONLY:BOOL=OFF\n", encoding="utf-8")
    hooks = DeliveryHooks(
        instruction="Train the CIFAR-10 model.",
        named_outputs=(),
        min_tools_for_completion_retry=99,
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "accept"


@pytest.mark.asyncio
async def test_cpu_only_retries_when_cmake_cache_is_off(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "caffe" / "build" / "CMakeCache.txt"
    cache.parent.mkdir(parents=True)
    cache.write_text("# cache\nCPU_ONLY:BOOL=OFF\n", encoding="utf-8")
    hooks = DeliveryHooks(
        instruction="Install Caffe 1.0.0 and build for only CPU execution.",
        named_outputs=(),
        min_tools_for_completion_retry=99,
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "instruction_cpu_only"
    cache.write_text("CPU_ONLY:BOOL=ON\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=3), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_cpu_only_makefile_beats_cmake_cache(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    caffe = tmp_path / "caffe"
    caffe.mkdir()
    (caffe / "Makefile.config").write_text("CPU_ONLY := 0\n", encoding="utf-8")
    cache = caffe / "build" / "CMakeCache.txt"
    cache.parent.mkdir()
    cache.write_text("CPU_ONLY:BOOL=ON\n", encoding="utf-8")
    hooks = DeliveryHooks(
        instruction="build for only CPU execution",
        named_outputs=(),
        min_tools_for_completion_retry=99,
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "retry"
    assert action.reason == "instruction_cpu_only"
    (caffe / "Makefile.config").write_text("CPU_ONLY := 1\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=3), LoopContext())
    assert done.kind == "accept"


@pytest.mark.asyncio
async def test_cpu_only_skips_when_instruction_omits_cpu(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "build" / "CMakeCache.txt"
    cache.parent.mkdir()
    cache.write_text("CPU_ONLY:BOOL=OFF\n", encoding="utf-8")
    hooks = DeliveryHooks(
        instruction="Train the CIFAR-10 model.",
        named_outputs=(),
        min_tools_for_completion_retry=99,
    )
    action = await hooks.before_completion(_draft(tools=2), LoopContext())
    assert action.kind == "accept"

