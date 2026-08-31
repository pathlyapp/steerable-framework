"""MCP server: expose the framework's tool surface over stdio (W3.2).

The client seam (``mcp.py``) lets the framework call out; this module is
the other half — letting other agents call *us*. Zero-dependency like the
client: newline-delimited JSON-RPC 2.0 over stdio, the MCP stdio
transport.

Two design points:

* **The tool surface is the router's.** ``tools/list`` projects
  ``ToolRouter.list_tools()``; ``tools/call`` dispatches through the same
  ``RouterToolExecutor`` the loop uses, so modes, consent gates, and
  shell-safety rules apply identically on the MCP boundary.
* **Approval does not collapse at the boundary (W3.2.2).** When the
  client advertises the ``elicitation`` capability, gated calls are
  decided by ``elicitation/create`` offering all six user-decidable
  variants of the approval lattice (allow/deny × once/session/always) —
  not a two-state allow/deny. ``abort`` and ``timed_out`` remain system
  outcomes, not user options. Clients without elicitation fail closed
  for gated calls (read-mode tools still pass, same as the ACP path).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .approval import (
    ApprovalDecision,
    ApprovalExecutor,
    ApprovalRequest,
    SessionApprovalCache,
)
from .loop import LoopContext, RouterToolExecutor
from .tools import ToolRouter

logger = logging.getLogger(__name__)

#: Protocol version this server speaks; we negotiate down to the client's.
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

#: The six user-decidable approval variants offered via elicitation.
#: (abort/timed_out are system outcomes, never menu items.)
_ELICITATION_OPTIONS: tuple[tuple[str, str], ...] = (
    ("allow_once", "Allow once"),
    ("allow_for_session", "Allow for this session"),
    ("allow_always", "Always allow"),
    ("deny_once", "Deny once"),
    ("deny_for_session", "Deny for this session"),
    ("deny_always", "Always deny"),
)


class JsonRpcEndpoint:
    """Bidirectional newline-delimited JSON-RPC over asyncio streams.

    One reader task dispatches inbound messages: requests/notifications to
    the server loop, responses to the pending map of outbound requests
    (elicitation round-trips interleave with inbound tool calls).
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._inbound: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._next_id = 0
        #: The pump reads continuously — a server-to-client request
        #: (elicitation) must have its response routed while the serve loop
        #: is blocked awaiting it inside a tools/call dispatch.
        self._pump = asyncio.create_task(self._pump_lines())

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        """Send a server-to-client request and await its result."""
        self._next_id += 1
        msg_id = self._next_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[msg_id] = future
        try:
            self._write({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
            return await future
        finally:
            self._pending.pop(msg_id, None)

    def respond(self, msg_id: Any, *, result: Any = None, error: dict[str, Any] | None = None) -> None:
        out: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            out["error"] = error
        else:
            out["result"] = result
        self._write(out)

    def _write(self, payload: dict[str, Any]) -> None:
        self._writer.write(json.dumps(payload, ensure_ascii=False).encode() + b"\n")

    async def _pump_lines(self) -> None:
        while True:
            line = await self._reader.readline()
            if not line:
                await self._inbound.put(None)
                return
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("MCP: dropping unparseable line (%d bytes)", len(line))
                continue
            if "method" not in message and "id" in message:
                future = self._pending.pop(message["id"], None)
                if future is not None and not future.done():
                    if "error" in message:
                        future.set_exception(McpClientError(message["error"]))
                    else:
                        future.set_result(message.get("result"))
                continue
            await self._inbound.put(message)

    async def read_message(self) -> dict[str, Any] | None:
        """Next inbound request/notification; responses are claimed by
        pending outbound requests and never surface here. None on EOF."""
        return await self._inbound.get()

    def close(self) -> None:
        self._pump.cancel()


class McpClientError(Exception):
    """The client returned a JSON-RPC error for a server-to-client request."""


class McpElicitationApprover:
    """``Approver`` over MCP ``elicitation/create`` (W3.2.2).

    Read-mode calls auto-allow (a client that listed the tool already
    consented to reads — same stance as the ACP path). Other modes offer
    the full six-variant lattice. Decline, cancel, unknown answers, and
    transport failure all map to ``deny_once`` — fail closed, never
    silently allow.
    """

    def __init__(self, endpoint: JsonRpcEndpoint, *, elicitation_supported: bool) -> None:
        self._endpoint = endpoint
        self._supported = elicitation_supported

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        if request.mode == "read":
            return ApprovalDecision("allow_once")
        if not self._supported:
            return ApprovalDecision(
                "deny_once",
                "MCP client has no elicitation capability; gated call refused",
            )
        try:
            result = await self._endpoint.request(
                "elicitation/create",
                {
                    "message": (
                        f"Tool {request.tool_name!r} ({request.mode}) requests "
                        f"approval. Arguments: {json.dumps(request.arguments, ensure_ascii=False)[:500]}"
                    ),
                    "requestedSchema": {
                        "type": "object",
                        "properties": {
                            "decision": {
                                "type": "string",
                                "enum": [option for option, _ in _ELICITATION_OPTIONS],
                                "description": " / ".join(
                                    f"{option} = {label}"
                                    for option, label in _ELICITATION_OPTIONS
                                ),
                            }
                        },
                        "required": ["decision"],
                    },
                },
            )
        except (McpClientError, OSError) as exc:
            logger.warning("elicitation failed: %s", exc)
            return ApprovalDecision("deny_once", f"elicitation failed: {exc}")
        action = (result or {}).get("action")
        if action != "accept":
            return ApprovalDecision("deny_once", f"elicitation {action or 'cancelled'}")
        chosen = ((result or {}).get("content") or {}).get("decision")
        valid = {option for option, _ in _ELICITATION_OPTIONS}
        if chosen in valid:
            return ApprovalDecision(chosen)
        logger.warning("elicitation returned unknown decision %r", chosen)
        return ApprovalDecision("deny_once", "elicitation returned an unknown decision")


class McpServer:
    """Serves one ToolRouter over the MCP stdio transport."""

    def __init__(
        self,
        router: ToolRouter,
        *,
        name: str = "steerable",
        version: str = "0.1.0",
    ) -> None:
        self._router = router
        self._name = name
        self._version = version
        self._elicitation_supported = False
        self._endpoint: JsonRpcEndpoint | None = None
        #: Session-scoped approval cache lives exactly as long as the MCP
        #: session — "for_session" variants mean *this* connection.
        self._approvals = SessionApprovalCache()

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._endpoint = JsonRpcEndpoint(reader, writer)
        try:
            while True:
                message = await self._endpoint.read_message()
                if message is None:
                    return
                method = message.get("method", "")
                msg_id = message.get("id")
                if msg_id is None:
                    # Notification (e.g. notifications/initialized) — no response.
                    continue
                try:
                    result = await self._dispatch(method, message.get("params") or {})
                except McpMethodError as exc:
                    self._endpoint.respond(msg_id, error={"code": exc.code, "message": str(exc)})
                    continue
                except Exception as exc:  # defensive: never kill the session loop
                    logger.exception("MCP %s failed", method)
                    self._endpoint.respond(
                        msg_id, error={"code": -32603, "message": f"internal error: {exc}"}
                    )
                    continue
                self._endpoint.respond(msg_id, result=result)
        finally:
            self._endpoint.close()

    async def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            capabilities = (params.get("capabilities") or {})
            self._elicitation_supported = "elicitation" in capabilities
            return {
                "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": self._name, "version": self._version},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.schema,
                    }
                    for tool in self._router.list_tools()
                    if tool.exposure != "hidden"
                ]
            }
        if method == "tools/call":
            return await self._call_tool(params)
        raise McpMethodError(f"unknown method: {method}", code=-32601)

    async def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise McpMethodError("tools/call requires a name", code=-32602)
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise McpMethodError("tools/call arguments must be an object", code=-32602)

        from steerable_agent_protocol.generated import ToolCall

        assert self._endpoint is not None
        executor: Any = RouterToolExecutor(self._router)
        executor = ApprovalExecutor(
            executor,
            McpElicitationApprover(
                self._endpoint, elicitation_supported=self._elicitation_supported
            ),
            session=self._approvals,
        )
        result = await executor.execute(
            ToolCall(id=f"mcp-{name}", name=name, arguments=arguments),
            LoopContext(),
        )
        text = (
            json.dumps(result.data, ensure_ascii=False)
            if result.data is not None
            else (result.error or result.message or "")
        )
        return {
            "content": [{"type": "text", "text": text}],
            "isError": not result.success,
        }


class McpMethodError(Exception):
    def __init__(self, message: str, *, code: int) -> None:
        super().__init__(message)
        self.code = code


async def serve_stdio(router: ToolRouter, **kwargs: Any) -> None:
    """Serve on the process's stdin/stdout — the MCP stdio transport."""
    import sys

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    await McpServer(router, **kwargs).serve(reader, writer)
