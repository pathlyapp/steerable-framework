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
    _is_script_listener,
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
    assert action.reason == "named_shown_text"
    assert "stub" in (action.message or "")
    dest.write_text("HELLO\n", encoding="utf-8")
    done = await hooks.before_completion(_draft(tools=5), LoopContext())
    assert done.kind == "accept"


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

