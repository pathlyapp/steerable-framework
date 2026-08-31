"""W1.5.2: shell session lifecycle — persistence, interaction, reclamation.

Real subprocesses throughout: the zombie-process risk this module exists
to control cannot be tested against mocks. All tests skip on Windows
(W4.1 owns the Windows spawn decision).
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from steerable_sidecar.shell_session import ShellSessionManager

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="bash sessions require a POSIX shell"
)


@pytest.fixture
def manager():
    mgr = ShellSessionManager()
    yield mgr
    mgr.close_all()


def _drain(mgr: ShellSessionManager, session_id: str, needle: str, timeout: float = 10.0):
    """Poll until ``needle`` appears in cumulative output, the session exits,
    or timeout. An empty needle waits for exit."""
    deadline = time.monotonic() + timeout
    seen = ""
    read = None
    while time.monotonic() < deadline:
        read = mgr.write_stdin(session_id, "", yield_ms=300)
        seen += read.output
        if (needle and needle in seen) or read.exited:
            return seen, read
    return seen, read


def test_open_runs_command_and_returns_output(manager, tmp_path: Path) -> None:
    read = manager.open(cwd=tmp_path, command="echo hello-session")
    out, _ = _drain(manager, read.session_id, "hello-session")
    assert "hello-session" in out


def test_state_persists_across_reads(manager, tmp_path: Path) -> None:
    read = manager.open(cwd=tmp_path, command="X=41")
    out, _ = _drain(manager, read.session_id, "$", timeout=5)
    manager.write_stdin(read.session_id, "echo $((X+1))\n")
    out, _ = _drain(manager, read.session_id, "42")
    assert "42" in out


def test_interactive_read_prompt_roundtrip(manager, tmp_path: Path) -> None:
    read = manager.open(
        cwd=tmp_path, command="read -r -p 'name: ' name; echo \"hi $name\""
    )
    _drain(manager, read.session_id, "name:", timeout=5)
    manager.write_stdin(read.session_id, "steerable\n")
    out, _ = _drain(manager, read.session_id, "hi steerable")
    assert "hi steerable" in out


def test_python_repl_the_newly_possible_task(manager, tmp_path: Path) -> None:
    """The W1.5 headline: a REPL session — impossible for one-shot bash."""
    read = manager.open(cwd=tmp_path, command="python3 -q -i")
    _drain(manager, read.session_id, ">>>", timeout=10)
    manager.write_stdin(read.session_id, "21*2\n")
    out, _ = _drain(manager, read.session_id, "42")
    assert "42" in out
    manager.write_stdin(read.session_id, "exit()\n")


def test_incremental_polling_of_long_running_command(manager, tmp_path: Path) -> None:
    # The needle is split in the command text so terminal echo of the command
    # itself cannot produce it — only the echo's output does.
    read = manager.open(cwd=tmp_path, command='sleep 2; echo fin""ished')
    assert "finished" not in read.output
    out, _ = _drain(manager, read.session_id, "finished", timeout=8)
    assert "finished" in out


def test_ctrl_c_interrupts_but_session_survives(manager, tmp_path: Path) -> None:
    read = manager.open(cwd=tmp_path)
    _drain(manager, read.session_id, "$")  # initial prompt: shell is up
    manager.write_stdin(read.session_id, "sleep 100\n")
    _drain(manager, read.session_id, "sleep 100")  # echo: command submitted
    manager.write_stdin(read.session_id, "\x03")
    # The needle is printf OUTPUT, not input: the tty echoes typed lines, so
    # an "echo still-alive" needle could false-positive off its own echo
    # while sleep is still running. "alive-OK" only exists if bash executed.
    manager.write_stdin(read.session_id, "printf 'alive-%s\\n' OK\n")
    out, _ = _drain(manager, read.session_id, "alive-OK")
    assert "alive-OK" in out


def test_exit_command_reports_exit_status(manager, tmp_path: Path) -> None:
    read = manager.open(cwd=tmp_path, command="exit 7")
    _, final = _drain(manager, read.session_id, "", timeout=5)
    assert final.exited
    assert final.exit_code == 7


def test_close_kills_the_whole_process_group(manager, tmp_path: Path) -> None:
    """A background child spawned by the session must not outlive close."""
    read = manager.open(cwd=tmp_path)
    marker = tmp_path / "child.pid"
    manager.write_stdin(
        read.session_id, f"sleep 300 & echo $! > {marker}\n"
    )
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    child_pid = int(marker.read_text().strip())
    manager.close(read.session_id)
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_close_all_reaps_everything(tmp_path: Path) -> None:
    mgr = ShellSessionManager()
    first = mgr.open(cwd=tmp_path, command="sleep 300")
    second = mgr.open(cwd=tmp_path, command="sleep 300")
    mgr.close_all()
    time.sleep(0.3)
    for session_id in (first.session_id, second.session_id):
        with pytest.raises(KeyError):
            mgr.write_stdin(session_id, "", yield_ms=10)


def test_idle_sessions_reaped_by_ttl(tmp_path: Path) -> None:
    mgr = ShellSessionManager(session_ttl_sec=0.5)
    try:
        read = mgr.open(cwd=tmp_path, command="sleep 300")
        time.sleep(0.8)
        with pytest.raises(KeyError):
            mgr.write_stdin(read.session_id, "", yield_ms=10)
    finally:
        mgr.close_all()


def test_session_limit_fails_loud(tmp_path: Path) -> None:
    mgr = ShellSessionManager(max_sessions=1)
    try:
        mgr.open(cwd=tmp_path)
        with pytest.raises(ValueError, match="session limit"):
            mgr.open(cwd=tmp_path)
    finally:
        mgr.close_all()


def test_unknown_session_fails_loud(manager) -> None:
    with pytest.raises(KeyError, match="unknown session"):
        manager.write_stdin("sh-nope", "", yield_ms=10)


def test_external_kill_is_reported_as_exited(manager, tmp_path: Path) -> None:
    """The shell dying out from under the manager (OOM, kill -9) must surface
    as exited on the next read, not hang the poll."""
    read = manager.open(cwd=tmp_path, command="sleep 300")
    shell_pid = manager._sessions[read.session_id].proc.pid
    os.kill(shell_pid, signal.SIGKILL)
    _, final = _drain(manager, read.session_id, "", timeout=5)
    assert final.exited
    assert final.exit_code != 0
