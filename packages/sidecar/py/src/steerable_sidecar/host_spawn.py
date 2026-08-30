"""Host-spawn tool execution (W2.2.1): confined spawn via reverse channel.

``SandboxedToolExecutor`` confines a shell call by *rewriting the command*
into a sandboxed invocation — which requires a platform rewriter (Seatbelt
on macOS, bwrap/landlock on Linux). Windows has no rewriter: real
confinement there is a restricted-token + JobObject spawn, and only the
host can do that (``CreateProcessWithTokenW`` needs the privileged parent,
not the sandboxed child). This executor is the no-rewriter route: the raw
command travels to the host over the reverse JSON-RPC channel as
``host.process.spawn`` with an explicit confinement policy, and the host
spawns it confined.

Contract (docs/spec/safety.md "Host capability surface"):

- request: ``{command, cwd?, policy: {writableRoots, network, allowedHosts},
  context?}``
- reply: ``{exitCode, stdout, stderr, truncated?, sandbox: {backend,
  enforcement}}`` — the host reports the enforcement it *actually* applied;
  the sidecar never upgrades a missing report beyond ``none``.
- fail closed: a host that doesn't implement the method answers with a
  JSON-RPC error and the call ends as a tool error — the command never runs
  unsandboxed just because the capability is absent.

Non-shell calls pass through to the inner executor untouched.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime.sandboxed import DEFAULT_SHELL_TOOLS

if TYPE_CHECKING:
    from collections.abc import Collection

    from steerable_agent_runtime.loop import LoopContext, ToolExecutor
    from steerable_agent_runtime.transport.stdio_jsonrpc import JsonRpcServer

logger = logging.getLogger(__name__)

#: Reverse-channel method name. Versioned with the contract doc.
HOST_SPAWN_METHOD = "host.process.spawn"


class HostSpawnExecutor:
    """Route shell-tool calls to the host's confined-spawn capability."""

    def __init__(
        self,
        inner: ToolExecutor,
        server: JsonRpcServer,
        *,
        policy: dict[str, Any],
        shell_tools: Collection[str] = DEFAULT_SHELL_TOOLS,
        command_arg: str = "command",
        timeout: float | None = None,
    ) -> None:
        self._inner = inner
        self._server = server
        self._policy = policy
        self._shell_tools = frozenset(shell_tools)
        self._command_arg = command_arg
        self._timeout = timeout

    def concurrency_safe(self, call: ToolCall) -> bool:
        return self._inner.concurrency_safe(call)

    async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
        if call.name not in self._shell_tools:
            return await self._inner.execute(call, ctx)
        command = (call.arguments or {}).get(self._command_arg)
        if not isinstance(command, str) or not command.strip():
            # Same convention as SandboxedToolExecutor: a shell call without
            # a command string is the inner executor's problem, not ours.
            return await self._inner.execute(call, ctx)

        params: dict[str, Any] = {
            "command": command,
            "policy": self._policy,
        }
        cwd = (call.arguments or {}).get("cwd")
        if isinstance(cwd, str) and cwd:
            params["cwd"] = cwd
        if ctx.chat_id:
            params["context"] = {"chatId": ctx.chat_id}
        try:
            payload: Any = await self._server.call(
                HOST_SPAWN_METHOD, params, timeout=self._timeout
            )
        except Exception as exc:  # noqa: BLE001 — fail closed, never run unsandboxed
            logger.warning("host spawn unavailable: %s", exc)
            return ToolResult(
                success=False,
                error=(
                    f"host confined spawn unavailable ({exc}); the command was "
                    "NOT run — refusing to fall back to unsandboxed execution "
                    "on a platform with no local sandbox backend"
                ),
                needsFollowup=True,
            )
        return _coerce_spawn_result(payload)


def _coerce_spawn_result(payload: Any) -> ToolResult:
    """Host reply → ToolResult. Unknown keys fold into ``data`` (same
    convention as HostToolExecutor); the sandbox marker comes from the
    host's own report, defaulting to the honest floor ``none``."""
    if not isinstance(payload, dict):
        return ToolResult(success=True, data={"value": payload})
    sandbox = payload.get("sandbox")
    if not isinstance(sandbox, dict) or "enforcement" not in sandbox:
        sandbox = {"backend": "host-spawn", "enforcement": "none"}
    data: dict[str, Any] = {
        k: v for k, v in payload.items() if k not in ("success", "error", "sandbox")
    }
    data["_sandbox"] = sandbox
    exit_code = payload.get("exitCode")
    success = payload.get("success")
    if success is None:
        success = exit_code == 0 if isinstance(exit_code, int) else True
    return ToolResult(
        success=bool(success),
        error=payload.get("error"),
        data=data,
        needsFollowup=True,
    )
