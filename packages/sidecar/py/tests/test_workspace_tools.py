from __future__ import annotations

import time
from pathlib import Path

import pytest
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime.errors import PolicyDeniedError

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
