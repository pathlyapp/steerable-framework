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
from typing import Any, cast

from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime import LoopContext
from steerable_agent_runtime.approval import (
    APPROVAL_KINDS,
    ApprovalDecision,
    ApprovalKind,
    ApprovalRequest,
)
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
        tool_context: dict[str, Any] | None = None,
    ) -> None:
        self._server = server
        self._method = method
        self._timeout = timeout
        # Embedder-supplied context forwarded on every reverse call (e.g.
        # {"mode": "plan"} so the host can hard-block write tools).
        self._tool_context = tool_context

    def concurrency_safe(self, call: ToolCall) -> bool:
        """Host tools always run serially: the reverse channel executes in
        the Electron host, which owns the real side effects (shell, files)
        and serializes them itself."""
        return False

    async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
        context: dict[str, Any] = {}
        if ctx.chat_id:
            context["chatId"] = ctx.chat_id
        if self._tool_context:
            context.update(self._tool_context)
        try:
            payload: Any = await self._server.call(
                self._method,
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments or {},
                    "context": context or None,
                },
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — host failure surfaces as tool error
            logger.warning("host tool %s failed: %s", call.name, exc)
            return ToolResult(success=False, error=str(exc), needsFollowup=True)

        if isinstance(payload, dict) and "success" in payload:
            return _coerce_host_result(payload)
        return ToolResult(success=True, data={"value": payload})


class HostApprover:
    """Approver over the reverse channel: the host UI answers
    ``approval.request`` with ``{"kind": <variant>, "reason": "..."}``.

    Fails closed: an unreachable host or an invalid reply becomes a
    ``deny_once`` — never a hang, never an auto-allow.

    Amendments (W2.4.2): a reply may also carry
    ``{"amendment": {"decision": "allow"|"deny", "commandPrefix": [...]}}``
    — "and keep applying this to matching calls". With an ``amendment_sink``
    wired, the amendment becomes an ``ApprovalRule`` handed to the sink
    (which updates the live policy and persists it); without one the
    amendment is dropped with a warning (the decision itself still stands).
    """

    def __init__(
        self,
        server: JsonRpcServer,
        *,
        method: str = "approval.request",
        timeout: float | None = None,
        amendment_sink: Any | None = None,
    ) -> None:
        self._server = server
        self._method = method
        self._timeout = timeout
        self._amendment_sink = amendment_sink

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        try:
            payload: Any = await self._server.call(
                self._method,
                {
                    "toolName": request.tool_name,
                    "arguments": request.arguments,
                    "mode": request.mode,
                    "category": request.category,
                    "round": request.round_index,
                },
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — fail closed
            logger.warning("host approval request failed: %s", exc)
            return ApprovalDecision(
                "deny_once", f"host approval unavailable: {exc}"
            )
        kind = payload.get("kind") if isinstance(payload, dict) else None
        if kind not in APPROVAL_KINDS:
            logger.warning("host returned invalid approval decision: %r", payload)
            return ApprovalDecision(
                "deny_once", f"host returned an invalid approval decision: {kind!r}"
            )
        if isinstance(payload, dict) and "amendment" in payload:
            self._apply_amendment(request, payload.get("amendment"))
        return ApprovalDecision(
            cast(ApprovalKind, kind),  # validated against APPROVAL_KINDS above
            str(payload.get("reason") or ""),
        )

    def _apply_amendment(self, request: ApprovalRequest, amendment: Any) -> None:
        from steerable_agent_runtime import rule_from_amendment

        rule = rule_from_amendment(request, amendment)
        if rule is None:
            return
        if self._amendment_sink is None:
            logger.warning(
                "host sent an approval amendment but no amendment sink is "
                "wired; dropping it (decision stands): %r",
                amendment,
            )
            return
        try:
            self._amendment_sink(rule)
        except OSError as exc:
            # The decision stands; only the persistence failed. Surface it —
            # a silently dropped amendment re-asks the user next time.
            logger.warning("failed to persist approval amendment: %s", exc)


_KNOWN_RESULT_KEYS = frozenset(ToolResult.model_fields)


def _coerce_host_result(payload: dict[str, Any]) -> ToolResult:
    """Build a ``ToolResult`` from the host's reply.

    The desktop host's tool router returns flat result objects (e.g.
    ``LocalExecResult`` carries ``stdout`` / ``stderr`` / ``exitCode`` at the
    top level). ``ToolResult`` only has ``data`` for payloads, so unknown
    keys are folded into it — otherwise pydantic's default ``extra="ignore"``
    silently drops the command output and the model sees only
    ``{"success": true}``.
    """
    extras = {k: v for k, v in payload.items() if k not in _KNOWN_RESULT_KEYS}
    if extras:
        payload = {k: v for k, v in payload.items() if k in _KNOWN_RESULT_KEYS}
        data = payload.get("data")
        payload["data"] = {**extras, **data} if isinstance(data, dict) else extras
    return ToolResult(**payload)
