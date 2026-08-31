from __future__ import annotations

import time
from pathlib import Path

import pytest
from steerable_agent_protocol.generated import ToolCall

from steerable_sidecar import workspace_tools as workspace_tools_mod
from steerable_sidecar.workspace_tools import (
    _MAX_OUTPUT,
    pgrep_self_wait,
    workspace_tools_for_cwd,
)


async def _call(router, name: str, arguments: dict) -> object:
    return await router.dispatch(
        ToolCall(id="t", name=name, arguments=arguments),
        consent_granted=True,
    )


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
    huge = "a" * (_MAX_OUTPUT + 10)
    (tmp_path / "big.txt").write_text(huge)
    read = await _call(router, "read_file", {"path": "big.txt"})
    assert read.success is True
    assert read.data["content"].endswith("...[truncated]...")
    binary = await _call(router, "bash", {"command": r"printf '\x99\xff'"})
    assert binary.success is True


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


def test_jailed_workspace_disables_sudo_gate(tmp_path: Path) -> None:
    open_router = workspace_tools_for_cwd(tmp_path)
    jailed = workspace_tools_for_cwd(tmp_path, jailed=True)
    assert open_router._shell_safety is None
    assert jailed._shell_safety is not None
    assert "sudo" in jailed._shell_safety.disabled_pattern_ids


def test_pgrep_self_wait_detects_while_loop() -> None:
    assert pgrep_self_wait("while pgrep -f install3.R; do sleep 2; done")
    assert pgrep_self_wait("while pgrep -af run_marginal.R\ndo\n  sleep 1\ndone")
    assert not pgrep_self_wait("pgrep -f install3.R")
    assert not pgrep_self_wait("wait $pid")
    assert not pgrep_self_wait("")


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
