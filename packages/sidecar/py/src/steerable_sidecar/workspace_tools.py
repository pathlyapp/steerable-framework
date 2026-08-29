"""In-process bash / file tools scoped to a workspace directory.

Harbor Terminal-Bench (and the ACP adapter's default surface) run the
CoreLoop *inside* the trial container. The editor-backed ACP fs/terminal
bridges are a separate follow-up; these tools are the product coding
surface for headless evals.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from steerable_agent_protocol.generated import ToolResult
from steerable_agent_runtime import ToolRouter

_MAX_OUTPUT = 32_768
_BASH_TIMEOUT_SEC = 120

_BASH_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "Shell command to run in the workspace"},
    },
    "required": ["command"],
}
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
    },
    "required": ["path", "content"],
}


def workspace_tools_for_cwd(cwd: str | Path) -> ToolRouter:
    """Return a router whose bash/read/write calls stay under ``cwd``."""
    root = Path(cwd).expanduser().resolve()
    router = ToolRouter()

    def bash(command: str = "", cmd: str = "", script: str = "") -> ToolResult:
        command = command or cmd or script
        if not command or not command.strip():
            return ToolResult(success=False, error="command is empty", needsFollowup=True)
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=_BASH_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                error=f"bash timed out after {_BASH_TIMEOUT_SEC}s",
                needsFollowup=True,
            )
        stdout = _clip(completed.stdout)
        stderr = _clip(completed.stderr)
        return ToolResult(
            success=completed.returncode == 0,
            data={
                "exitCode": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
            },
            error=None if completed.returncode == 0 else f"exit {completed.returncode}",
            needsFollowup=completed.returncode != 0,
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
            data={"path": str(target), "content": clipped},
        )

    def write_file(path: str, content: str) -> ToolResult:
        try:
            target = _resolve_under(root, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except (OSError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc), needsFollowup=True)
        return ToolResult(
            success=True,
            data={"path": str(target), "bytes": len(content.encode("utf-8"))},
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
    return router


def _resolve_under(root: Path, path: str) -> Path:
    if not path or not str(path).strip():
        raise ValueError("path is empty")
    raw = Path(path)
    target = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"path escapes workspace: {path}")
    return target


def _clip(text: str) -> str:
    if len(text) <= _MAX_OUTPUT:
        return text
    return text[:_MAX_OUTPUT] + "\n...[truncated]..."
