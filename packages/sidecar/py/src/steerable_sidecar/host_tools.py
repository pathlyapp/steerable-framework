"""Host-routed tool execution for the CoreLoop chat path.

In the desktop deployment the tools (shell, files, MCP) live in the Electron
host, not in the sidecar process. ``HostToolExecutor`` implements the
runtime's ``ToolExecutor`` port by forwarding each call to the host over the
A1 reverse JSON-RPC channel and coercing the reply back into a ``ToolResult``.

Selected per request with ``toolsViaHost: true``; without it the CoreLoop
path keeps using the sidecar-local registry (``RouterToolExecutor``).

Note: the framework ToolRouter's extras (unknown-tool suggestions, shell
safety gate) are bypassed on this path — the host's own router is expected to
enforce them (deeppath-agent's ToolRouter carries the same 61-rule set).
"""

from __future__ import annotations

import logging
from typing import Any

from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime import LoopContext
from steerable_agent_runtime.transport.stdio_jsonrpc import JsonRpcServer

logger = logging.getLogger(__name__)


class HostToolExecutor:
    """Route tool calls to the host via reverse-channel ``tool.invoke``."""

    def __init__(
        self,
        server: JsonRpcServer,
        *,
        method: str = "tool.invoke",
        timeout: float | None = None,
    ) -> None:
        self._server = server
        self._method = method
        self._timeout = timeout

    async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
        try:
            payload: Any = await self._server.call(
                self._method,
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments or {},
                    "context": {"chatId": ctx.chat_id} if ctx.chat_id else None,
                },
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — host failure surfaces as tool error
            logger.warning("host tool %s failed: %s", call.name, exc)
            return ToolResult(success=False, error=str(exc), needsFollowup=True)

        if isinstance(payload, dict) and "success" in payload:
            return ToolResult(**payload)
        return ToolResult(success=True, data={"value": payload})
