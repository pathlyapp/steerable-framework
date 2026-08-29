from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from steerable_agent_protocol.generated import ToolCall

from steerable_sidecar.workspace_tools import (
    _BASH_TIMEOUT_SEC,
    _MAX_OUTPUT,
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
async def test_clip_and_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    huge = "a" * (_MAX_OUTPUT + 10)
    (tmp_path / "big.txt").write_text(huge)
    read = await _call(router, "read_file", {"path": "big.txt"})
    assert read.success is True
    assert read.data["content"].endswith("...[truncated]...")

    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="sleep", timeout=_BASH_TIMEOUT_SEC)

    monkeypatch.setattr(subprocess, "run", _timeout)
    timed = await _call(router, "bash", {"command": "sleep 999"})
    assert timed.success is False
    assert "timed out" in (timed.error or "")
