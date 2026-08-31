"""ACP terminal client bridge (3.4.3.2): one-shot bash on the editor's PTY.

When the client advertises the ``terminal`` capability, the ``bash`` tool
runs on the editor's terminal — the user sees the command and its output
in their own terminal panel, and the process lives in the client's
environment. The five ACP callbacks form one lifecycle:

    create_terminal → wait_for_terminal_exit → terminal_output → release
                      ↳ timeout: kill_terminal first

That lifecycle is the W1.5 ownership contract applied to a client-backed
handle — live ids are tracked, a fail-loud cap bounds them, and
``release_all()`` at prompt teardown releases whatever survived, exactly
the discipline ``ShellSessionManager.close_all`` gives local sessions.
There is precisely one interactive session layer (W1.5's PTY manager):
ACP terminals have no stdin channel, so ``bash_session`` / ``write_stdin``
stay local — an honest protocol gap, not a silent downgrade.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from steerable_agent_protocol.generated import ToolResult

_MAX_TERMINALS = 8


class AcpTerminalRunner:
    """``BashRunner`` over the ACP client's terminal methods."""

    def __init__(
        self,
        conn: Any,
        session_id: str,
        *,
        timeout_sec: float = 300.0,
        max_output: int = 32_768,
        max_terminals: int = _MAX_TERMINALS,
    ) -> None:
        self._conn = conn
        self._session_id = session_id
        self._timeout_sec = timeout_sec
        self._max_output = max_output
        self._max_terminals = max_terminals
        self._live: set[str] = set()
        self._guard = asyncio.Lock()

    async def run(self, command: str, cwd: Path) -> ToolResult:
        async with self._guard:
            if len(self._live) >= self._max_terminals:
                return ToolResult(
                    success=False,
                    error=(
                        f"terminal limit reached ({self._max_terminals}); "
                        "let a running command finish first"
                    ),
                    needsFollowup=True,
                )
        try:
            created = await self._conn.create_terminal(
                self._session_id,
                "bash",
                args=["-c", command],
                cwd=str(cwd),
                output_byte_limit=self._max_output,
            )
        except Exception as exc:  # client transport/refusal — fail loud
            return ToolResult(
                success=False,
                error=f"ACP terminal/create failed: {exc}",
                needsFollowup=True,
            )
        terminal_id = str(created.terminal_id)
        async with self._guard:
            self._live.add(terminal_id)
        timed_out = False
        exit_code: int | None = None
        try:
            try:
                exited = await asyncio.wait_for(
                    self._conn.wait_for_terminal_exit(self._session_id, terminal_id),
                    timeout=self._timeout_sec,
                )
                exit_code = exited.exit_code
            except asyncio.TimeoutError:
                timed_out = True
                try:
                    await self._conn.kill_terminal(self._session_id, terminal_id)
                except Exception as exc:  # kill is best-effort; output still follows
                    return ToolResult(
                        success=False,
                        error=(
                            f"bash timed out after {self._timeout_sec}s and "
                            f"ACP terminal/kill failed: {exc}"
                        ),
                        needsFollowup=True,
                    )
            output_resp = await self._conn.terminal_output(
                self._session_id, terminal_id
            )
            text = str(output_resp.output or "")
            if output_resp.truncated:
                text += "\n...[truncated by client]..."
        except Exception as exc:  # client transport — fail loud
            return ToolResult(
                success=False,
                error=f"ACP terminal output/wait failed: {exc}",
                needsFollowup=True,
            )
        finally:
            async with self._guard:
                self._live.discard(terminal_id)
            try:
                await self._conn.release_terminal(self._session_id, terminal_id)
            except Exception:  # release is hygiene; the run already concluded
                pass
        if timed_out:
            return ToolResult(
                success=False,
                error=f"bash timed out after {self._timeout_sec}s",
                needsFollowup=True,
            )
        return ToolResult(
            success=exit_code == 0,
            data={
                "exitCode": exit_code,
                "stdout": text,
                "stderr": "",
            },
            error=None if exit_code == 0 else f"exit {exit_code}",
            needsFollowup=exit_code != 0,
        )

    async def release_all(self) -> None:
        """Release every terminal still tracked — prompt-teardown hygiene,
        the client-backed counterpart of ``ShellSessionManager.close_all``."""
        async with self._guard:
            live = list(self._live)
            self._live.clear()
        for terminal_id in live:
            try:
                await self._conn.release_terminal(self._session_id, terminal_id)
            except Exception:  # teardown is best-effort; the client may be gone
                pass
