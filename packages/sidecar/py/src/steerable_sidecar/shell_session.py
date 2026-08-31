"""Persistent interactive shell sessions (W1.5).

The one-shot ``bash`` tool runs a command to completion and destroys the
process — REPLs, ssh, interactive installers (y/n prompts), and debuggers
score zero against it. This module is the session layer modeled on codex's
``exec_command`` / ``write_stdin`` split:

- ``open`` spawns a long-lived shell in its own process group and returns
  a session id plus the first chunk of output.
- ``write_stdin`` writes to the session's stdin (answers prompts, sends
  Ctrl-C) and returns the output produced since the caller's last read.
- ``close`` / ``close_all`` reap process groups so no grandchildren leak
  when the loop exits abnormally (the zombie risk called out in the plan).

Output is collected by a daemon reader thread into a per-session buffer;
each read returns bytes since that session's cursor and advances it.
There is no prompt-detection magic: the model polls with ``write_stdin``
and judges from the output whether the command finished, exactly the
codex contract.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SessionRead:
    """One read from a session: the new output and the liveness signal."""

    session_id: str
    output: str
    exited: bool
    exit_code: int | None
    total_bytes: int


@dataclass(slots=True)
class _Session:
    id: str
    proc: subprocess.Popen[bytes]
    master_fd: int
    buf: bytearray
    cursor: int = 0
    exited: bool = False
    exit_code: int | None = None
    last_activity: float = field(default_factory=time.monotonic)
    lock: threading.Condition = field(default_factory=lambda: threading.Condition())


class ShellSessionManager:
    """Owns the live sessions of one run.

    ``max_sessions`` fails loud instead of silently growing process
    tables; ``session_ttl_sec`` reaps idle sessions on every operation so
    a forgotten session cannot outlive its usefulness.
    """

    def __init__(
        self,
        *,
        shell: str = "/bin/bash",
        max_sessions: int = 8,
        session_ttl_sec: float = 1800.0,
    ) -> None:
        self._shell = shell
        self._max_sessions = max_sessions
        self._ttl = session_ttl_sec
        self._sessions: dict[str, _Session] = {}
        self._guard = threading.Lock()

    def open(
        self,
        *,
        cwd: Path,
        command: str | None = None,
        yield_ms: int = 1000,
        max_output: int = 30_000,
    ) -> SessionRead:
        """Spawn a session shell; run ``command`` if given."""
        with self._guard:
            self._reap_idle_locked()
            if len(self._sessions) >= self._max_sessions:
                raise ValueError(
                    f"session limit reached ({self._max_sessions}); "
                    "close a session before opening another"
                )
        # A PTY, not pipes: interactive semantics (prompts, Ctrl-C through the
        # line discipline, programs probing isatty) only exist on a terminal.
        # Imported here so the module loads on Windows (no pty there); the
        # failure lands at session open, loud, until W4.1 settles Windows spawn.
        import pty

        master_fd, slave_fd = pty.openpty()
        try:
            proc = subprocess.Popen(
                [self._shell, "--norc", "-i"],
                cwd=str(cwd),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                env={**os.environ, "TERM": "dumb"},
            )
        finally:
            os.close(slave_fd)
        session = _Session(
            id=f"sh-{uuid.uuid4().hex[:8]}",
            proc=proc,
            master_fd=master_fd,
            buf=bytearray(),
        )
        threading.Thread(
            target=self._reader, args=(session,), daemon=True
        ).start()
        with self._guard:
            self._sessions[session.id] = session
        if command:
            self._write(session, command + "\n")
        return self._read(session, yield_ms=yield_ms, max_output=max_output)

    def write_stdin(
        self,
        session_id: str,
        chars: str,
        *,
        yield_ms: int = 1000,
        max_output: int = 30_000,
    ) -> SessionRead:
        """Write to stdin (empty string polls) and read new output."""
        session = self._get(session_id)
        if chars:
            self._write(session, chars)
        return self._read(session, yield_ms=yield_ms, max_output=max_output)

    def close(self, session_id: str) -> None:
        with self._guard:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            self._kill(session)

    def close_all(self) -> None:
        """Reap every session — called at loop teardown, including
        abnormal exits, so process groups never outlive the run."""
        with self._guard:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            self._kill(session)

    # -- internals ------------------------------------------------------------

    def _get(self, session_id: str) -> _Session:
        with self._guard:
            self._reap_idle_locked()
            session = self._sessions.get(session_id)
        if session is None:
            known = sorted(self._sessions)
            raise KeyError(f"unknown session {session_id!r}; live: {known}")
        return session

    @staticmethod
    def _reader(session: _Session) -> None:
        fd = session.master_fd
        while True:
            try:
                chunk = os.read(fd, 65_536)
            except OSError:
                # EIO: the child exited and the slave side closed (POSIX).
                break
            if not chunk:
                break
            with session.lock:
                session.buf.extend(chunk)
                session.lock.notify_all()
        session.proc.wait()
        os.close(fd)
        with session.lock:
            session.exited = True
            session.exit_code = session.proc.returncode
            session.lock.notify_all()

    def _write(self, session: _Session, chars: str) -> None:
        if session.exited:
            raise ValueError(f"session {session.id} has exited")
        try:
            os.write(session.master_fd, chars.encode())
        except OSError as exc:
            raise ValueError(f"session {session.id} stdin closed: {exc}") from exc
        session.last_activity = time.monotonic()

    def _read(self, session: _Session, *, yield_ms: int, max_output: int) -> SessionRead:
        deadline = time.monotonic() + yield_ms / 1000.0
        with session.lock:
            while len(session.buf) == session.cursor and not session.exited:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                session.lock.wait(timeout=remaining)
            start = session.cursor
            end = len(session.buf)
            session.cursor = end
            exited = session.exited
            exit_code = session.exit_code
        text = bytes(session.buf[start:end]).decode("utf-8", errors="replace")
        if len(text) > max_output:
            text = text[:max_output] + "\n...[truncated]..."
        return SessionRead(
            session_id=session.id,
            output=text,
            exited=exited,
            exit_code=exit_code,
            total_bytes=end,
        )

    def _reap_idle_locked(self) -> None:
        now = time.monotonic()
        stale = [
            sid
            for sid, session in self._sessions.items()
            if session.exited or now - session.last_activity > self._ttl
        ]
        for sid in stale:
            session = self._sessions.pop(sid)
            self._kill(session)

    @staticmethod
    def _kill(session: _Session) -> None:
        """Reap the whole login session, not only the shell's process group.

        Interactive bash runs with job control on, so ``cmd &`` gets its
        *own* process group and a plain ``killpg(shell)`` leaks it. Order:
        SIGHUP the shell (interactive bash re-HUPs its jobs and exits),
        SIGKILL the shell's group, then sweep any surviving members of the
        session (same sid) via ps.
        """
        pid = session.proc.pid
        if session.proc.poll() is None:
            try:
                os.kill(pid, signal.SIGHUP)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                session.proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
        if session.proc.poll() is None and hasattr(os, "killpg"):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if session.proc.poll() is None:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        _sweep_session_members(pid)
        try:
            session.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL landed
            pass
        with session.lock:
            session.exited = True
            if session.exit_code is None:
                session.exit_code = session.proc.returncode
            session.lock.notify_all()


def _sweep_session_members(session_leader_pid: int) -> None:
    """Best-effort SIGKILL of every process whose session id is the dead
    shell's — catches background job groups that outlived killpg."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,sid="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, sid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if sid == session_leader_pid and pid != session_leader_pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
