from __future__ import annotations

import asyncio
import base64
import struct
import time
import zlib
from pathlib import Path

import pytest
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime.errors import PolicyDeniedError

from steerable_sidecar import workspace_tools as workspace_tools_mod
from steerable_sidecar.workspace_tools import (
    _BASH_SCHEMA,
    _MAX_OUTPUT,
    pgrep_self_wait,
    refuse_truncated_overwrite,
    short_timeout_wrap,
    sleep_poll,
    workspace_tools_for_cwd,
)


async def _call(router, name: str, arguments: dict) -> object:
    return await router.dispatch(
        ToolCall(id="t", name=name, arguments=arguments),
        consent_granted=True,
    )


def _gray_png(width: int, height: int, rows: list[list[int]]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _bgr_bmp(width: int, height: int, pixels: list[list[tuple[int, int, int]]]) -> bytes:
    stride = ((width * 3 + 3) // 4) * 4
    body = bytearray()
    for y in range(height - 1, -1, -1):
        row = bytearray()
        for r, g, b in pixels[y]:
            row.extend((b, g, r))
        row.extend(b"\x00" * (stride - width * 3))
        body.extend(row)
    header = struct.pack("<2sIHHI", b"BM", 54 + len(body), 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(body), 0, 0, 0, 0)
    return header + dib + bytes(body)


@pytest.mark.asyncio
async def test_bash_read_write_roundtrip(tmp_path: Path) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    written = await _call(
        router, "write_file", {"path": "nested/a.txt", "content": "hello"}
    )
    assert written.success is True
    read = await _call(router, "read_file", {"path": "nested/a.txt"})
    assert read.success is True
    assert read.data["content"] == "hello"
    bash = await _call(router, "bash", {"command": "cat nested/a.txt && echo ok"})
    assert bash.success is True
    assert "hello" in bash.data["stdout"]
    assert "ok" in bash.data["stdout"]


@pytest.mark.asyncio
async def test_grep_glob_apply_patch_wired(tmp_path: Path) -> None:
    """W1.4.1 wiring: the three structured tools dispatch through the router."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def alpha():\n    return 1\n")
    (tmp_path / "src" / "b.py").write_text("def beta():\n    return 2\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.py").write_text("alpha junk\n")

    router = workspace_tools_for_cwd(tmp_path)
    names = {t.get("name") or t.get("function", {}).get("name") for t in router.describe_model()}
    assert {"grep", "glob", "apply_patch"} <= names

    hits = await _call(router, "grep", {"query": "alpha"})
    assert hits.success is True
    assert [h["path"] for h in hits.data["hits"]] == ["src/a.py"]  # junk ignored

    paths = await _call(router, "glob", {"pattern": "**/*.py"})
    assert paths.success is True
    assert sorted(paths.data["paths"]) == ["src/a.py", "src/b.py"]

    patched = await _call(
        router,
        "apply_patch",
        {
            "patches": [
                {
                    "path": "src/a.py",
                    "edits": [{"oldText": "return 1", "newText": "return 10"}],
                },
                {
                    "path": "src/b.py",
                    "edits": [{"oldText": "return 2", "newText": "return 20"}],
                },
            ]
        },
    )
    assert patched.success is True
    assert sorted(patched.data["filesChanged"]) == ["src/a.py", "src/b.py"]
    assert "return 10" in (tmp_path / "src" / "a.py").read_text()
    assert "return 20" in (tmp_path / "src" / "b.py").read_text()


@pytest.mark.asyncio
async def test_apply_patch_escape_rejected(tmp_path: Path) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    result = await _call(
        router,
        "apply_patch",
        {
            "patches": [
                {
                    "path": "../outside.txt",
                    "edits": [{"oldText": "x", "newText": "y"}],
                }
            ]
        },
    )
    assert result.success is False
    assert result.needsFollowup is True


@pytest.mark.asyncio
async def test_grep_invalid_regex_fails_loud(tmp_path: Path) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    result = await _call(router, "grep", {"query": "([", "isRegex": True})
    assert result.success is False
    assert "invalid regex" in result.error


@pytest.mark.asyncio
async def test_bash_session_roundtrip_via_router(tmp_path: Path) -> None:
    """W1.5 wiring: open → command → poll → close through the tool surface."""
    router = workspace_tools_for_cwd(tmp_path)
    try:
        opened = await _call(
            router, "bash_session", {"command": "echo sess-ready", "yieldMs": 3000}
        )
        assert opened.success is True
        session_id = opened.data["sessionId"]
        assert "sess-ready" in opened.data["output"]

        # Poll until the command's output lands: the first read returns the
        # terminal's echo of the input, not the result.
        seen = ""
        for _ in range(20):
            polled = await _call(
                router,
                "write_stdin",
                {
                    "sessionId": session_id,
                    "chars": "echo poll-$((6*7))\n" if not seen else "",
                    "yieldMs": 500,
                },
            )
            assert polled.success is True
            seen += polled.data["output"]
            if "poll-42" in seen:
                break
        assert "poll-42" in seen

        closed = await _call(
            router, "write_stdin", {"sessionId": session_id, "close": True}
        )
        assert closed.success is True and closed.data["closed"] is True
    finally:
        router.shell_sessions.close_all()


@pytest.mark.asyncio
async def test_write_stdin_unknown_session_fails_loud(tmp_path: Path) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    result = await _call(
        router, "write_stdin", {"sessionId": "sh-nonexistent", "chars": ""}
    )
    assert result.success is False
    assert result.needsFollowup is True


@pytest.mark.asyncio
async def test_bash_empty_and_nonzero(tmp_path: Path) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    empty = await _call(router, "bash", {"command": "  "})
    assert empty.success is False
    missing = await _call(router, "bash", {})
    assert missing.success is False
    assert "empty" in (missing.error or "")
    aliased = await _call(router, "bash", {"cmd": "echo aliased"})
    assert aliased.success is True
    assert "aliased" in aliased.data["stdout"]
    failed = await _call(router, "bash", {"command": "exit 7"})
    assert failed.success is False
    assert failed.data["exitCode"] == 7


@pytest.mark.asyncio
async def test_path_escape_and_empty(tmp_path: Path) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    escaped = await _call(router, "read_file", {"path": "../outside.txt"})
    assert escaped.success is False
    assert "escapes" in (escaped.error or "")
    blank = await _call(router, "write_file", {"path": "", "content": "x"})
    assert blank.success is False


@pytest.mark.asyncio
async def test_missing_file(tmp_path: Path) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    missing = await _call(router, "read_file", {"path": "nope.txt"})
    assert missing.success is False


@pytest.mark.asyncio
async def test_clip_and_binary_stdout(tmp_path: Path) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    huge = "H" * 40 + "M" * (_MAX_OUTPUT) + "T" * 40
    (tmp_path / "big.txt").write_text(huge)
    read = await _call(router, "read_file", {"path": "big.txt"})
    assert read.success is True
    content = read.data["content"]
    assert content.startswith("H" * 40)
    assert content.endswith("T" * 40)
    assert "truncated" in content
    assert len(content) <= _MAX_OUTPUT + 80
    binary = await _call(router, "bash", {"command": r"printf '\x99\xff'"})
    assert binary.success is True
    png = tmp_path / "board.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    as_text = await _call(router, "read_file", {"path": "board.png"})
    assert as_text.success is False
    assert "PNG" in (as_text.error or "")
    assert "PIL" in (as_text.error or "")
    good = tmp_path / "gray.png"
    good.write_bytes(_gray_png(4, 2, [[0, 0, 255, 255], [0, 0, 255, 255]]))
    preview = await _call(router, "read_file", {"path": "gray.png"})
    assert preview.success is True
    assert preview.data["kind"] == "png_ascii"
    assert "PNG 4x2" in preview.data["content"]
    assert "_image" not in preview.data
    assert "mean-brightness" not in preview.data["content"]
    square = [[40 if (y // 10 + x // 10) % 2 == 0 else 90 for x in range(80)] for y in range(80)]
    for y in range(70, 80):
        for x in range(0, 10):
            square[y][x] = 0 if (x + y) % 2 == 0 else 255
    board = tmp_path / "board80.png"
    board.write_bytes(_gray_png(80, 80, square))
    board_preview = await _call(router, "read_file", {"path": "board80.png"})
    assert board_preview.success is True
    assert "PNG 80x80 ASCII preview (80x80)" in board_preview.data["content"]
    assert "Rank 8 at top" in board_preview.data["content"]
    assert "a b c d e f g h" in board_preview.data["content"]
    assert "8 |" in board_preview.data["content"]
    assert "occupancy" in board_preview.data["content"]
    assert "#" in board_preview.data["content"]
    assert "occupied squares:" in board_preview.data["content"]
    assert "a1" in board_preview.data["content"]
    bmp = tmp_path / "frame.bmp"
    bmp.write_bytes(
        _bgr_bmp(4, 2, [[(0, 0, 0), (0, 0, 0), (255, 255, 255), (255, 255, 255)]] * 2)
    )
    bmp_preview = await _call(router, "read_file", {"path": "frame.bmp"})
    assert bmp_preview.success is True
    assert bmp_preview.data["kind"] == "bmp_ascii"
    assert "BMP 4x2" in bmp_preview.data["content"]
    junk = tmp_path / "junk.bmp"
    junk.write_bytes(b"BM" + b"\xff" * 40)
    junk_read = await _call(router, "read_file", {"path": "junk.bmp"})
    assert junk_read.success is False
    assert "BMP" in (junk_read.error or "")
    jpeg_path = Path(__file__).with_name("half.jpg")
    jpeg = tmp_path / "invoice.jpg"
    jpeg.write_bytes(jpeg_path.read_bytes())
    jpeg_preview = await _call(router, "read_file", {"path": "invoice.jpg"})
    assert jpeg_preview.success is True
    assert jpeg_preview.data["kind"] == "jpeg_ascii"
    assert "JPEG 16x8" in jpeg_preview.data["content"]
    assert "_image" not in jpeg_preview.data
    junk_jpeg = tmp_path / "junk.jpg"
    junk_jpeg.write_bytes(b"\xff\xd8\xff\x00" + b"\xff" * 40)
    junk_jpeg_read = await _call(router, "read_file", {"path": "junk.jpg"})
    assert junk_jpeg_read.success is False
    assert "JPEG" in (junk_jpeg_read.error or "")


@pytest.mark.asyncio
async def test_read_file_attaches_png_pixels_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code Read sends pixels; ASCII-only is why code-from-image
    brute-forced a hash that missed ``bee26a``."""
    monkeypatch.setenv("STEERABLE_READ_IMAGES", "1")
    router = workspace_tools_for_cwd(tmp_path)
    png = tmp_path / "code.png"
    raw = _gray_png(4, 2, [[0, 0, 255, 255], [0, 0, 255, 255]])
    png.write_bytes(raw)
    read = await _call(router, "read_file", {"path": "code.png"})
    assert read.success is True
    assert read.data["kind"] == "png_ascii"
    blob = read.data["_image"]
    assert blob["media_type"] == "image/png"
    assert blob["b64"] == base64.b64encode(raw).decode("ascii")
    bmp = tmp_path / "frame.bmp"
    bmp.write_bytes(
        _bgr_bmp(4, 2, [[(0, 0, 0), (0, 0, 0), (255, 255, 255), (255, 255, 255)]] * 2)
    )
    bmp_read = await _call(router, "read_file", {"path": "frame.bmp"})
    assert bmp_read.success is True
    assert "_image" not in bmp_read.data
    jpeg_path = Path(__file__).with_name("half.jpg")
    jpeg = tmp_path / "invoice.jpg"
    jpeg.write_bytes(jpeg_path.read_bytes())
    jpeg_read = await _call(router, "read_file", {"path": "invoice.jpg"})
    assert jpeg_read.success is True
    assert jpeg_read.data["_image"]["media_type"] == "image/jpeg"
    monkeypatch.setenv("STEERABLE_READ_IMAGES", "1")
    oversize = b"\x89PNG\r\n\x1a\n" + b"\x00" * 400_001
    assert workspace_tools_mod._image_blob(oversize) is None


@pytest.mark.asyncio
async def test_bash_timeout_kills_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_tools_mod, "_BASH_TIMEOUT_SEC", 1)
    router = workspace_tools_for_cwd(tmp_path)
    started = time.monotonic()
    timed = await _call(router, "bash", {"command": "sleep 30 | cat"})
    elapsed = time.monotonic() - started
    assert timed.success is False
    assert "timed out" in (timed.error or "")
    assert elapsed < 10


@pytest.mark.asyncio
async def test_bash_cancel_kills_pipeline(tmp_path: Path) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    started = time.monotonic()
    task = asyncio.create_task(
        _call(router, "bash", {"command": "sleep 30 | cat"})
    )
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 5


@pytest.mark.asyncio
async def test_bash_background_job_survives_return(tmp_path: Path) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    marker = tmp_path / "later.txt"
    started = await _call(
        router,
        "bash",
        {"command": f"(sleep 1; echo ok > {marker}) &"},
    )
    assert started.success is True
    time.sleep(1.4)
    assert marker.read_text() == "ok\n"


def test_jailed_workspace_disables_sudo_gate(tmp_path: Path) -> None:
    open_router = workspace_tools_for_cwd(tmp_path)
    jailed = workspace_tools_for_cwd(tmp_path, jailed=True)
    assert open_router._shell_safety is None
    assert jailed._shell_safety is not None
    disabled = set(jailed._shell_safety.disabled_pattern_ids)
    assert {"sudo", "dd_if", "dd", "mkfs"} <= disabled


@pytest.mark.asyncio
async def test_jailed_allows_dd_if_and_tmp_write(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    jailed = workspace_tools_for_cwd(tmp_path, jailed=True)
    dd = await _call(
        jailed,
        "bash",
        {"command": "dd if=/dev/zero of=disk.img bs=1024 count=1 2>/dev/null"},
    )
    assert dd.success is True
    assert (tmp_path / "disk.img").is_file()
    outside = tmp_path_factory.mktemp("outside-jail") / "result.txt"
    written = await _call(
        jailed, "write_file", {"path": str(outside), "content": "ok"}
    )
    assert written.success is True
    assert outside.read_text() == "ok"
    with pytest.raises(PolicyDeniedError):
        await _call(jailed, "bash", {"command": "rm -rf /"})
    blocked = workspace_tools_for_cwd(tmp_path)
    escaped = await _call(
        blocked, "write_file", {"path": str(outside), "content": "no"}
    )
    assert escaped.success is False
    assert "escapes" in (escaped.error or "")


def test_pgrep_self_wait_detects_while_loop() -> None:
    assert pgrep_self_wait("while pgrep -f install3.R; do sleep 2; done")
    assert pgrep_self_wait("while pgrep -af run_marginal.R\ndo\n  sleep 1\ndone")
    assert not pgrep_self_wait("pgrep -f install3.R")
    assert not pgrep_self_wait("wait $pid")
    assert not pgrep_self_wait("")


def test_sleep_poll_detects_long_sleep_then_cat() -> None:
    assert sleep_poll("sleep 290; cat /tmp/out.log")
    assert sleep_poll("sleep 120 && tail -f log")
    assert not sleep_poll("sleep 5; ls")
    assert not sleep_poll("sleep 300")
    assert not sleep_poll("cmd & pid=$!; wait \"$pid\"; cat out")


def test_short_timeout_wrap_detects_vm_compile() -> None:
    assert short_timeout_wrap("timeout 120 node vm.js")
    assert short_timeout_wrap("timeout 60 make -C /app all")
    assert not short_timeout_wrap("timeout 10 curl -I http://localhost")
    assert not short_timeout_wrap("timeout 3600 node vm.js")
    assert short_timeout_wrap("timeout 10 qemu-system-x86_64 -nographic")
    assert not short_timeout_wrap("timeout 3600 qemu-system-x86_64 -nographic")


def test_bash_schema_warns_against_short_timeout() -> None:
    desc = _BASH_SCHEMA["properties"]["command"]["description"]
    assert "timeout N" in desc
    assert "3600s" in desc


@pytest.mark.asyncio
async def test_bash_refuses_pgrep_self_wait(tmp_path: Path) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    refused = await _call(
        router,
        "bash",
        {"command": "while pgrep -f hung.sh; do sleep 1; done"},
    )
    assert refused.success is False
    assert "pgrep" in (refused.error or "")
    assert "wait" in (refused.error or "")


@pytest.mark.asyncio
async def test_bash_refuses_sleep_poll(tmp_path: Path) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    refused = await _call(
        router, "bash", {"command": "sleep 290; cat /tmp/out.log"}
    )
    assert refused.success is False
    assert "sleep" in (refused.error or "")
    assert "wait" in (refused.error or "")


@pytest.mark.asyncio
async def test_bash_refuses_short_timeout_wrap(tmp_path: Path) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    refused = await _call(
        router, "bash", {"command": "timeout 120 node vm.js"}
    )
    assert refused.success is False
    assert "timeout" in (refused.error or "")
    assert "3600s" in (refused.error or "")


def test_refuse_truncated_overwrite_thresholds() -> None:
    assert refuse_truncated_overwrite(8192, 100) is True
    assert refuse_truncated_overwrite(8192, 4096) is False
    assert refuse_truncated_overwrite(100, 10) is False


@pytest.mark.asyncio
async def test_write_file_refuses_shrinking_large_file(tmp_path: Path) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    big = "row\n" * 3000
    written = await _call(router, "write_file", {"path": "sample.csv", "content": big})
    assert written.success is True
    refused = await _call(
        router, "write_file", {"path": "sample.csv", "content": "row\nonly\n"}
    )
    assert refused.success is False
    assert "Refusing to overwrite" in (refused.error or "")
    assert (tmp_path / "sample.csv").read_text() == big
    grown = await _call(
        router, "write_file", {"path": "sample.csv", "content": big + "extra\n"}
    )
    assert grown.success is True


# ── read-before-write state (CC readFileState / seed_read_state parity) ──


@pytest.mark.asyncio
async def test_read_file_state_tracks_reads_and_writes(tmp_path: Path) -> None:
    state: dict[str, str] = {}
    router = workspace_tools_for_cwd(tmp_path, read_file_state=state)
    written = await _call(router, "write_file", {"path": "a.txt", "content": "v1"})
    assert written.success is True
    # Our own write records the fresh version.
    assert state[str(tmp_path / "a.txt")] == written.data["version"]
    read = await _call(router, "read_file", {"path": "a.txt"})
    assert read.success is True
    assert state[str(tmp_path / "a.txt")] == read.data["version"]


@pytest.mark.asyncio
async def test_auto_cas_rejects_write_after_external_modification(
    tmp_path: Path,
) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    await _call(router, "write_file", {"path": "a.txt", "content": "v1"})
    await _call(router, "read_file", {"path": "a.txt"})
    # Modified outside the session — the tracked version is now stale.
    (tmp_path / "a.txt").write_text("changed outside", encoding="utf-8")
    rejected = await _call(router, "write_file", {"path": "a.txt", "content": "v2"})
    assert rejected.success is False
    assert "冲突" in rejected.error
    # Re-reading refreshes the state; the retry passes.
    await _call(router, "read_file", {"path": "a.txt"})
    ok = await _call(router, "write_file", {"path": "a.txt", "content": "v2"})
    assert ok.success is True


@pytest.mark.asyncio
async def test_auto_cas_allows_sequential_writes_after_own_write(
    tmp_path: Path,
) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    await _call(router, "write_file", {"path": "a.txt", "content": "v1"})
    second = await _call(router, "write_file", {"path": "a.txt", "content": "v2"})
    assert second.success is True


@pytest.mark.asyncio
async def test_auto_cas_edit_file_rejects_stale_edit(tmp_path: Path) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    await _call(router, "write_file", {"path": "a.txt", "content": "hello world"})
    (tmp_path / "a.txt").write_text("hello there world", encoding="utf-8")
    rejected = await _call(
        router,
        "edit_file",
        {"path": "a.txt", "edits": [{"oldText": "world", "newText": "moon"}]},
    )
    assert rejected.success is False
    assert "冲突" in rejected.error


@pytest.mark.asyncio
async def test_new_file_creation_never_gated(tmp_path: Path) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    created = await _call(router, "write_file", {"path": "new.txt", "content": "x"})
    assert created.success is True


@pytest.mark.asyncio
async def test_require_read_before_write_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Default-ON (CC parity): no env needed. An existing unread file rejects
    # overwrite; new files create freely; a read clears the gate.
    monkeypatch.delenv("STEERABLE_REQUIRE_READ_BEFORE_WRITE", raising=False)
    (tmp_path / "existing.txt").write_text("old", encoding="utf-8")
    router = workspace_tools_for_cwd(tmp_path)
    blocked = await _call(
        router, "write_file", {"path": "existing.txt", "content": "new"}
    )
    assert blocked.success is False
    assert "尚未读过" in blocked.error
    # New files still create freely.
    created = await _call(router, "write_file", {"path": "fresh.txt", "content": "x"})
    assert created.success is True
    # After a read the write passes.
    await _call(router, "read_file", {"path": "existing.txt"})
    ok = await _call(router, "write_file", {"path": "existing.txt", "content": "new"})
    assert ok.success is True
    # edit_file is gated the same way on an unread existing file.
    (tmp_path / "other.txt").write_text("old", encoding="utf-8")
    blocked_edit = await _call(
        router,
        "edit_file",
        {"path": "other.txt", "edits": [{"oldText": "old", "newText": "new"}]},
    )
    assert blocked_edit.success is False
    assert "尚未读过" in blocked_edit.error


@pytest.mark.asyncio
async def test_require_read_before_write_opt_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STEERABLE_REQUIRE_READ_BEFORE_WRITE", "0")
    (tmp_path / "existing.txt").write_text("old", encoding="utf-8")
    router = workspace_tools_for_cwd(tmp_path)
    ok = await _call(router, "write_file", {"path": "existing.txt", "content": "new"})
    assert ok.success is True


@pytest.mark.asyncio
async def test_partial_view_rejects_full_overwrite_but_allows_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A file larger than the display clip is read partially: the model never
    # saw the tail, so a blind full overwrite is rejected (CC isPartialView
    # parity) while a CAS-checked targeted edit stays allowed.
    monkeypatch.delenv("STEERABLE_REQUIRE_READ_BEFORE_WRITE", raising=False)
    from steerable_sidecar.workspace_tools import _MAX_OUTPUT

    big = "line\n" * (_MAX_OUTPUT // 5 + 1000)
    (tmp_path / "big.txt").write_text(big, encoding="utf-8")
    router = workspace_tools_for_cwd(tmp_path)

    read = await _call(router, "read_file", {"path": "big.txt"})
    assert read.success is True
    assert read.data["partial"] is True
    assert len(read.data["content"]) < len(big)

    blocked = await _call(router, "write_file", {"path": "big.txt", "content": "new"})
    assert blocked.success is False
    assert "只读到部分内容" in blocked.error

    edited = await _call(
        router,
        "edit_file",
        {"path": "big.txt", "edits": [{"oldText": "line\n", "newText": "LINE\n"}]},
    )
    assert edited.success is True

    # A small file reads in full (partial: false) and overwrites freely.
    (tmp_path / "small.txt").write_text("small", encoding="utf-8")
    read_small = await _call(router, "read_file", {"path": "small.txt"})
    assert read_small.data["partial"] is False
    ok = await _call(router, "write_file", {"path": "small.txt", "content": "new"})
    assert ok.success is True


@pytest.mark.asyncio
async def test_apply_patch_updates_read_state(tmp_path: Path) -> None:
    state: dict[str, str] = {}
    router = workspace_tools_for_cwd(tmp_path, read_file_state=state)
    await _call(router, "write_file", {"path": "a.txt", "content": "hello world"})
    patched = await _call(
        router,
        "apply_patch",
        {
            "patches": [
                {"path": "a.txt", "edits": [{"oldText": "world", "newText": "moon"}]}
            ]
        },
    )
    assert patched.success is True
    assert patched.data["versions"] == {
        str(tmp_path / "a.txt"): state[str(tmp_path / "a.txt")]
    }
    # A later write auto-CASes against the post-patch version, not the stale
    # pre-patch one.
    ok = await _call(router, "write_file", {"path": "a.txt", "content": "final"})
    assert ok.success is True


def test_read_file_state_from_messages() -> None:
    import json as _json

    from steerable_agent_runtime.llm import LLMMessage

    from steerable_sidecar.workspace_tools import read_file_state_from_messages

    messages = [
        LLMMessage.text_of("user", "go"),
        LLMMessage.text_of(
            "tool",
            _json.dumps(
                {
                    "success": True,
                    "data": {"path": "/w/a.txt", "version": "v1", "content": "…"},
                }
            ),
            name="read_file",
        ),
        # A later write supersedes the read's version.
        LLMMessage.text_of(
            "tool",
            _json.dumps(
                {"success": True, "data": {"path": "/w/a.txt", "version": "v2"}}
            ),
            name="write_file",
        ),
        LLMMessage.text_of(
            "tool",
            _json.dumps(
                {
                    "success": True,
                    "data": {
                        "filesChanged": ["b.txt"],
                        "versions": {"/w/b.txt": "v3"},
                    },
                }
            ),
            name="apply_patch",
        ),
        # Spilled/folded bodies and non-file tools are skipped.
        LLMMessage.text_of("tool", "spilled pointer, not json", name="read_file"),
        LLMMessage.text_of(
            "tool", _json.dumps({"success": True, "data": {"value": 1}}), name="bash"
        ),
    ]
    state = read_file_state_from_messages(messages)
    assert state == {"/w/a.txt": "v2", "/w/b.txt": "v3"}
