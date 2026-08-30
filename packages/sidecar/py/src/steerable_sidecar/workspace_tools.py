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
from .png_ascii import ascii_png_preview

_MAX_OUTPUT = 100_000
# Catalog-89 bn-fit-modify: a no_artifact retry overwrote a 10k-row sample
# with two write_file rows. Refuse shrinking an already-large file that far.
_MIN_KEEP_BYTES = 8192
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
                "Shell command to run in the workspace. Timeout is 3600s; do "
                "not poll with `sleep 290`. Do not wrap compile, VM, or train "
                "in `timeout N` with N under 300 — bash already caps at 3600s. "
                "Do not wait with `while pgrep -f ...` (pgrep matches the "
                "wait loop). Background long jobs and `wait $!`, or poll a "
                "pidfile."
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
# Catalog-89 mteb-leaderboard: `sleep 290; cat log` under the old 300s bash
# cap. Bash now waits 3600s — `wait $!` instead of a long sleep-then-cat.
_SLEEP_POLL = re.compile(
    r"\bsleep\s+(\d+)\s*(?:;|&&|\n)\s*(?:cat|tail|ls|head)\b",
    re.IGNORECASE,
)
_SLEEP_POLL_MIN_SEC = 120
_SLEEP_POLL_ERROR = (
    "Refusing a long `sleep N; cat/tail/ls` poll. Bash already waits up to "
    "3600s. Background the job (`cmd & pid=$!`) and `wait \"$pid\"`."
)
# Catalog-89 make-doom-for-mips: `timeout 120 node vm.js` killed the VM
# before /tmp/frame.bmp existed. Bash already caps at 3600s.
_SHORT_TIMEOUT = re.compile(
    r"\btimeout\s+(?:--signal=\S+\s+|-[A-Za-z]\s+\S+\s+)*(\d+)\s+"
    r"(?=[^;\n]{0,80}\b(?:node|make|gcc|g\+\+|rustc|clang\+\+|clang)\b)",
    re.IGNORECASE,
)
_SHORT_TIMEOUT_MAX_SEC = 299
_SHORT_TIMEOUT_ERROR = (
    "Refusing `timeout N` around compile/VM with N under 300s. Bash already "
    "caps at 3600s. Drop the timeout wrapper so the job can finish."
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
        "content": {
            "type": "string",
            "description": (
                "Full file contents to write. Do not replace an existing "
                "complete output with a truncated body."
            ),
        },
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


# Harbor/headless already runs inside the trial container. Keep host-killing
# rules (rm -rf /, fork bomb, chmod -R 777 /). Allow TB disk-image work:
# password-recovery and qemu tasks use ``dd if=`` / mkfs as ordinary steps.
_JAILED_DISABLED_SAFETY = ("sudo", "dd_if", "dd", "mkfs")


def workspace_tools_for_cwd(cwd: str | Path, *, jailed: bool = False) -> ToolRouter:
    """Return a router whose bash/read/write calls stay under ``cwd``.

    ``jailed=True`` is for Harbor/headless: the process already runs inside
    the trial container, so ``sudo`` and ``dd if=`` are normal TB agent steps,
    not host privilege escalation. ``rm -rf /`` stays critical. File tools
    may write anywhere in the container (hidden tests name ``/tmp`` and
    ``/app``); non-jailed ACP sessions stay cwd-scoped.
    """
    root = Path(cwd).expanduser().resolve()
    safety = (
        CommandSafetyConfig(disabled_pattern_ids=list(_JAILED_DISABLED_SAFETY))
        if jailed
        else None
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

    async def bash(command: str = "", cmd: str = "", script: str = "") -> ToolResult:
        command = command or cmd or script
        if not command or not command.strip():
            return ToolResult(success=False, error="command is empty", needsFollowup=True)
        if pgrep_self_wait(command):
            return ToolResult(
                success=False, error=_PGREP_WAIT_ERROR, needsFollowup=True
            )
        if sleep_poll(command):
            return ToolResult(
                success=False, error=_SLEEP_POLL_ERROR, needsFollowup=True
            )
        if short_timeout_wrap(command):
            return ToolResult(
                success=False, error=_SHORT_TIMEOUT_ERROR, needsFollowup=True
            )
        # Off the event loop so CoreLoop parallel_tools can overlap bash.
        return await asyncio.to_thread(_run_bash, command)

    def _run_bash(command: str) -> ToolResult:
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
            target = _resolve_under(root, path, jailed=jailed)
            raw = target.read_bytes()
        except (OSError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc), needsFollowup=True)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            preview = ascii_png_preview(raw)
            if preview is not None:
                return ToolResult(
                    success=True,
                    data={
                        "path": str(target),
                        "content": preview,
                        "kind": (
                            "bmp_ascii" if preview.startswith("BMP ") else "png_ascii"
                        ),
                    },
                )
            kind = (
                "PNG"
                if raw.startswith(b"\x89PNG")
                else "JPEG"
                if raw[:2] == b"\xff\xd8"
                else "BMP"
                if raw[:2] == b"BM"
                else "binary"
            )
            return ToolResult(
                success=False,
                error=(
                    f"{target} is {kind} ({len(raw)} bytes), not UTF-8 text. "
                    "Decode it with Python (PIL/numpy) or `file`; do not guess "
                    "contents from the filename."
                ),
                needsFollowup=True,
            )
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
            target = _resolve_under(root, path, jailed=jailed)
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
                shrink = _truncated_overwrite_error(target, content)
                if shrink:
                    return ToolResult(
                        success=False, error=shrink, needsFollowup=True
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
            target = _resolve_under(root, path, jailed=jailed)
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


def refuse_truncated_overwrite(existing_bytes: int, new_bytes: int) -> bool:
    """True when replacing a large file with a much smaller body."""
    if existing_bytes < _MIN_KEEP_BYTES:
        return False
    return new_bytes < max(512, existing_bytes // 4)


def _truncated_overwrite_error(target: Path, content: str) -> str | None:
    try:
        existing = target.stat().st_size
    except OSError:
        return None
    new_len = len(content.encode("utf-8"))
    if not refuse_truncated_overwrite(existing, new_len):
        return None
    return (
        f"Refusing to overwrite {existing}-byte {target} with {new_len} bytes. "
        "The existing file is already complete. Use edit_file for a patch, or "
        "bash to replace it after reading the current contents."
    )


def pgrep_self_wait(command: str) -> bool:
    """True when ``command`` is a ``while pgrep -f`` wait that matches itself."""
    return bool(_PGREP_SELF_WAIT.search(command or ""))


def sleep_poll(command: str) -> bool:
    """True when ``command`` is a long sleep followed by cat/tail/ls/head."""
    for match in _SLEEP_POLL.finditer(command or ""):
        if int(match.group(1)) >= _SLEEP_POLL_MIN_SEC:
            return True
    return False


def short_timeout_wrap(command: str) -> bool:
    """True when ``command`` wraps compile/VM in ``timeout N`` with N under 300s."""
    for match in _SHORT_TIMEOUT.finditer(command or ""):
        if int(match.group(1)) <= _SHORT_TIMEOUT_MAX_SEC:
            return True
    return False


def _resolve_under(root: Path, path: str, *, jailed: bool = False) -> Path:
    if not path or not str(path).strip():
        raise ValueError("path is empty")
    raw = Path(path)
    target = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    if not jailed and target != root and root not in target.parents:
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
    """Bound tool output without dropping the tail.

    Catalog-89 train/compile logs put the accuracy line, linker error, or
    qemu boot banner at the end. A prefix-only clip (then spill of that
    prefix) hid those lines, so the model stopped on a truncated success
    or never saw the failure.
    """
    if len(text) <= _MAX_OUTPUT:
        return text
    head = _MAX_OUTPUT // 5
    tail = _MAX_OUTPUT - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n...[{omitted} chars truncated]...\n{text[-tail:]}"
