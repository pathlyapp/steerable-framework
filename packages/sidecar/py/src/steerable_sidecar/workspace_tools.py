"""In-process bash / file tools scoped to a workspace directory.

Harbor Terminal-Bench (and the ACP adapter's default surface) run the
CoreLoop *inside* the trial container. The editor-backed ACP fs/terminal
bridges are a separate follow-up; these tools are the product coding
surface for headless evals.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import re
import signal
import subprocess
from pathlib import Path

from steerable_agent_harness.safety import CommandSafetyConfig
from steerable_agent_protocol.generated import ToolResult
from steerable_agent_runtime import ToolRouter

from .file_edit import EditError, EditOp, apply_edits, content_version

_MAX_OUTPUT = 100_000
# TB compiles, QEMU, and training exceed the old 5 min cap; Claude Code
# does not kill a single bash at 300s. Harbor's long-task kill is ~180 min.
_BASH_TIMEOUT_SEC = 3600
_tmp_counter = itertools.count()

_BASH_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": (
                "Shell command to run in the workspace. Do not wait with "
                "`while pgrep -f ...` (pgrep matches the wait loop). "
                "Background long jobs and `wait $!`, or poll a pidfile."
            ),
        },
    },
    "required": ["command"],
}
# `while pgrep -f <script>` matches the wait loop itself and never exits.
_PGREP_SELF_WAIT = re.compile(
    r"while\b[\s\S]{0,80}?\bpgrep\s+-[a-zA-Z]*f\b",
    re.IGNORECASE,
)
_PGREP_WAIT_ERROR = (
    "Refusing `while pgrep -f ...`: pgrep matches this wait loop and never "
    "exits. Background the job (`cmd & pid=$!`) and `wait \"$pid\"`, or poll "
    "a pidfile."
)
_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "File path relative to the workspace"},
    },
    "required": ["path"],
}
_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "File path relative to the workspace"},
        "content": {"type": "string", "description": "Full file contents to write"},
        "expectedVersion": {
            "type": "string",
            "description": (
                "Optional. The `version` returned by your last read_file of this "
                "path; when provided the write is rejected if the file changed since."
            ),
        },
    },
    "required": ["path", "content"],
}
_EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "File path relative to the workspace"},
        "edits": {
            "type": "array",
            "description": "One or more targeted replacements, applied together.",
            "items": {
                "type": "object",
                "properties": {
                    "oldText": {
                        "type": "string",
                        "description": "Exact snippet to replace; include enough context to be unique.",
                    },
                    "newText": {"type": "string", "description": "Replacement text."},
                },
                "required": ["oldText", "newText"],
            },
        },
        "expectedVersion": {
            "type": "string",
            "description": (
                "Optional. The `version` returned by your last read_file of this "
                "path; when provided the edit is rejected on conflict."
            ),
        },
    },
    "required": ["path", "edits"],
}


def workspace_tools_for_cwd(cwd: str | Path, *, jailed: bool = False) -> ToolRouter:
    """Return a router whose bash/read/write calls stay under ``cwd``.

    ``jailed=True`` is for Harbor/headless: the process already runs inside
    the trial container, so ``sudo`` is a normal TB agent step, not a host
    privilege escalation. ``rm -rf /`` stays critical.
    """
    root = Path(cwd).expanduser().resolve()
    safety = (
        CommandSafetyConfig(disabled_pattern_ids=["sudo"]) if jailed else None
    )
    router = ToolRouter(shell_safety=safety)
    # Per-path serialisation for read-modify-write, so concurrent edits to the
    # same file cannot interleave (pi's file-mutation-queue). Keyed by resolved
    # path; distinct files proceed independently.
    file_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(target: Path) -> asyncio.Lock:
        key = str(target)
        lock = file_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            file_locks[key] = lock
        return lock

    def _atomic_write(target: Path, content: str) -> None:
        tmp = target.with_name(
            f"{target.name}.tmp-{os.getpid()}-{next(_tmp_counter)}"
        )
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _check_version(target: Path, expected: str) -> str | None:
        try:
            current = target.read_text(encoding="utf-8")
        except OSError:
            return (
                f"无法冲突检测：读取 {target} 失败（文件可能不存在）。"
                "若确认要新建，请去掉 expectedVersion。"
            )
        if content_version(current) != expected:
            return (
                f"冲突：{target} 在你读取后已被修改（版本令牌不匹配）。为避免覆盖"
                "他人/其它操作的改动，本次写入已拒绝——请重新 read_file 拿到最新"
                "内容与 version，再基于它重新编辑。"
            )
        return None

    def bash(command: str = "", cmd: str = "", script: str = "") -> ToolResult:
        command = command or cmd or script
        if not command or not command.strip():
            return ToolResult(success=False, error="command is empty", needsFollowup=True)
        if pgrep_self_wait(command):
            return ToolResult(
                success=False, error=_PGREP_WAIT_ERROR, needsFollowup=True
            )
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=_BASH_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc.pid)
            try:
                proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
            return ToolResult(
                success=False,
                error=f"bash timed out after {_BASH_TIMEOUT_SEC}s",
                needsFollowup=True,
            )
        return ToolResult(
            success=proc.returncode == 0,
            data={
                "exitCode": proc.returncode,
                "stdout": _clip(stdout or ""),
                "stderr": _clip(stderr or ""),
            },
            error=None if proc.returncode == 0 else f"exit {proc.returncode}",
            needsFollowup=proc.returncode != 0,
        )

    def read_file(path: str) -> ToolResult:
        try:
            target = _resolve_under(root, path)
            text = target.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc), needsFollowup=True)
        clipped = _clip(text)
        return ToolResult(
            success=True,
            data={
                "path": str(target),
                "content": clipped,
                # Version of the FULL content (not the clipped preview), so a
                # read-before-write token stays valid even on large files.
                "version": content_version(text),
            },
        )

    async def write_file(
        path: str, content: str, expectedVersion: str | None = None
    ) -> ToolResult:
        try:
            target = _resolve_under(root, path)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc), needsFollowup=True)
        async with _lock_for(target):
            try:
                if expectedVersion is not None:
                    conflict = _check_version(target, expectedVersion)
                    if conflict:
                        return ToolResult(
                            success=False, error=conflict, needsFollowup=True
                        )
                target.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(target, content)
            except (OSError, ValueError) as exc:
                return ToolResult(success=False, error=str(exc), needsFollowup=True)
            return ToolResult(
                success=True,
                data={
                    "path": str(target),
                    "bytes": len(content.encode("utf-8")),
                    "version": content_version(content),
                },
            )

    async def edit_file(
        path: str, edits: list[dict], expectedVersion: str | None = None
    ) -> ToolResult:
        try:
            target = _resolve_under(root, path)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc), needsFollowup=True)
        ops = [
            EditOp(old_text=str(e.get("oldText", "")), new_text=str(e.get("newText", "")))
            for e in edits
            if isinstance(e, dict)
        ]
        async with _lock_for(target):
            try:
                current = target.read_text(encoding="utf-8")
            except OSError as exc:
                return ToolResult(success=False, error=str(exc), needsFollowup=True)
            if expectedVersion is not None:
                conflict = _check_version(target, expectedVersion)
                if conflict:
                    return ToolResult(success=False, error=conflict, needsFollowup=True)
            try:
                result = apply_edits(current, ops, file_path=target.name)
            except EditError as exc:
                return ToolResult(success=False, error=str(exc), needsFollowup=True)
            try:
                _atomic_write(target, result.content)
            except OSError as exc:
                return ToolResult(success=False, error=str(exc), needsFollowup=True)
            return ToolResult(
                success=True,
                data={
                    "path": str(target),
                    "version": content_version(result.content),
                    "diff": result.diff,
                    "applied": len(result.matches),
                    "matches": [
                        {
                            "level": m.level,
                            "startLine": m.start_line,
                            "oldLineCount": m.old_line_count,
                        }
                        for m in result.matches
                    ],
                },
            )

    router.register(
        bash,
        name="bash",
        mode="other",
        description="Run a shell command in the workspace directory.",
        schema=_BASH_SCHEMA,
        require_consent=False,
        metadata={"shell_command_param": "command"},
    )
    router.register(
        read_file,
        name="read_file",
        mode="read",
        description="Read a UTF-8 text file from the workspace.",
        schema=_READ_SCHEMA,
        require_consent=False,
    )
    router.register(
        write_file,
        name="write_file",
        mode="safe_write",
        description="Write a UTF-8 text file in the workspace (creates parents).",
        schema=_WRITE_SCHEMA,
        require_consent=False,
    )
    router.register(
        edit_file,
        name="edit_file",
        mode="safe_write",
        description=(
            "Make targeted edits to an existing file WITHOUT rewriting it whole. "
            "Each edit's oldText is located (exact → whitespace-tolerant → "
            "Unicode-normalised) and replaced by newText; all edits match against "
            "the original, must not overlap, and apply together. Prefer this over "
            "write_file when modifying existing files."
        ),
        schema=_EDIT_SCHEMA,
        require_consent=False,
    )
    return router


def pgrep_self_wait(command: str) -> bool:
    """True when ``command`` is a ``while pgrep -f`` wait that matches itself."""
    return bool(_PGREP_SELF_WAIT.search(command or ""))


def _resolve_under(root: Path, path: str) -> Path:
    if not path or not str(path).strip():
        raise ValueError("path is empty")
    raw = Path(path)
    target = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"path escapes workspace: {path}")
    return target


def _kill_process_group(pid: int) -> None:
    """SIGKILL the bash session, not only the shell, so pipelines release pipes."""
    if hasattr(os, "killpg"):
        try:
            os.killpg(pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _clip(text: str) -> str:
    if len(text) <= _MAX_OUTPUT:
        return text
    return text[:_MAX_OUTPUT] + "\n...[truncated]..."
