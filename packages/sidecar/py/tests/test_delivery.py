from __future__ import annotations

import os

import pytest
from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime.hooks import CompletionDraft
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.loop import LoopContext

from steerable_sidecar.delivery import (
    DeliveryGatedExecutor,
    DeliveryHooks,
    named_output_paths,
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
    titled = named_output_paths(
        "Output the primers in a fasta file titled primers.fasta."
    )
    assert "/app/primers.fasta" in titled
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
    src.write_bytes(bytes(range(256)) * 40)
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
async def test_named_checker_retries_once_when_check_py_exists(tmp_path) -> None:
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
    assert action.kind == "retry"
    assert action.reason == "named_checker"
    assert "check.py" in (action.message or "")
    again = await hooks.before_completion(_draft(tools=5), LoopContext())
    assert again.kind == "accept"


@pytest.mark.asyncio
async def test_named_checker_retries_once_when_eval_py_exists(tmp_path) -> None:
    dest = tmp_path / "eigen.py"
    (tmp_path / "eval.py").write_text("print('ok')\n", encoding="utf-8")
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
