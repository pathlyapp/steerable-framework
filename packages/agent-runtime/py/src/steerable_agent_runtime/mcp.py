"""MCP client seam: qualified names, capped catalogs, minimal stdio client.

MCP is the largest unbounded, third-party, mutable source of model-visible
context an agent system can add, so it lands on the Wave 2 foundation:
per-tool timeouts (Wave 0), exposure tiers (step 3), and the two rules
this module owns — deterministic name qualification and per-server catalog
caps.

* **Qualification** — every MCP tool is registered as
  ``mcp__<server>__<tool>`` (codex and DeepSeek Harness converged on the
  same shape independently), so name collisions between servers are
  impossible by construction and a tool's origin is visible to the model,
  the trace, and policy.
* **Catalog caps** — a server advertising more than ``max_tools`` tools
  fails loud at registration (`McpCatalogError`), never silently
  truncates: silent truncation would strand tools the model can neither
  see nor discover.
* **Exposure** — catalogs register as ``deferred`` by default: the model
  finds them through the ``tool_search`` seam instead of paying for every
  schema in every request. Hosts pin favorites with ``exposure="direct"``.

Architecture (recorded in docs/roadmap.md): in the desktop product, MCP
servers are launched host-side (Electron main) and reached through
`ToolRouter.register_remote` — a Seatbelt-confined sidecar never spawns
them. `McpStdioClient` exists for hosts that embed the runtime directly
(CLI agents, tests, non-Electron hosts); it is transport-only and holds no
loop state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .errors import RuntimeError as SteerableRuntimeError
from .tools import RegisteredTool, ToolExposure, ToolRouter

logger = logging.getLogger(__name__)

#: Deterministic qualification shape: ``mcp__<server>__<tool>``.
MCP_NAME_PREFIX = "mcp"
MCP_NAME_SEPARATOR = "__"

#: Provider function-name cap (OpenAI rejects longer names); qualified
#: names must fit.
MAX_TOOL_NAME_LENGTH = 64

#: Default per-server catalog cap. One server advertising more tools than
#: this is a misconfiguration (or a hostile server), not a workload.
DEFAULT_MAX_SERVER_TOOLS = 64

#: Protocol version offered in the initialize handshake. Servers
#: negotiate down if they are older.
DEFAULT_PROTOCOL_VERSION = "2025-06-18"


class McpError(SteerableRuntimeError):
    """MCP transport or protocol failure."""


class McpCatalogError(McpError):
    """A server's tool catalog violated a registration invariant."""


def qualify_mcp_name(server: str, tool: str) -> str:
    """Deterministic ``mcp__<server>__<tool>`` qualification.

    Server names may not contain the separator (it would make parsing
    ambiguous); tool names keep their separators — parsing splits with a
    limit, so ``mcp__github__create__issue`` round-trips.
    """
    if not server or MCP_NAME_SEPARATOR in server:
        raise McpCatalogError(
            f"MCP server name must be non-empty and contain no "
            f"'{MCP_NAME_SEPARATOR}': {server!r}"
        )
    if not tool:
        raise McpCatalogError(f"MCP tool name must be non-empty (server {server!r})")
    qualified = (
        f"{MCP_NAME_PREFIX}{MCP_NAME_SEPARATOR}{server}{MCP_NAME_SEPARATOR}{tool}"
    )
    if len(qualified) > MAX_TOOL_NAME_LENGTH:
        raise McpCatalogError(
            f"Qualified MCP tool name exceeds {MAX_TOOL_NAME_LENGTH} chars: "
            f"{qualified!r} — shorten the server alias"
        )
    return qualified


def parse_mcp_name(qualified: str) -> tuple[str, str] | None:
    """Inverse of `qualify_mcp_name`; None for non-MCP tool names."""
    parts = qualified.split(MCP_NAME_SEPARATOR, 2)
    if len(parts) != 3 or parts[0] != MCP_NAME_PREFIX or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


@dataclass(frozen=True, slots=True)
class McpToolInfo:
    """One tool from a server's ``tools/list`` catalog."""

    name: str
    description: str = ""
    schema: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )


@dataclass(frozen=True, slots=True)
class McpCallResult:
    """Coerced ``tools/call`` payload.

    ``text`` joins the result's text parts; non-text parts (images,
    resources) appear as ``[mcp <type> content]`` placeholders so the
    model knows something was elided. ``structured`` carries the server's
    ``structuredContent`` verbatim when present.
    """

    text: str
    is_error: bool = False
    structured: Any = None


def register_mcp_catalog(
    router: ToolRouter,
    *,
    server: str,
    tools: Sequence[McpToolInfo],
    invoker: Callable[[str, dict[str, Any]], Awaitable[Any]],
    max_tools: int = DEFAULT_MAX_SERVER_TOOLS,
    exposure: ToolExposure = "deferred",
) -> list[RegisteredTool]:
    """Register one server's catalog on ``router`` with qualified names.

    ``invoker`` is ``(unqualified_tool_name, arguments) -> result`` — the
    same delegation shape as `ToolRouter.register_remote`, so a host-side
    MCP client reached over the reverse channel and an in-process
    `McpStdioClient` plug in identically.

    Fails loud on an over-cap catalog or an unqualifiable name; a
    half-registered catalog never results (qualification is validated for
    every tool before any registration happens).
    """
    if len(tools) > max_tools:
        raise McpCatalogError(
            f"MCP server {server!r} advertises {len(tools)} tools, over the "
            f"per-server cap of {max_tools} — raise the cap deliberately or "
            f"fix the server"
        )
    qualified = [(qualify_mcp_name(server, t.name), t) for t in tools]
    registered: list[RegisteredTool] = []
    for qualified_name, info in qualified:
        original_name = info.name

        async def _mcp_handler(
            _invoker: Callable[[str, dict[str, Any]], Awaitable[Any]] = invoker,
            _tool: str = original_name,
            **kwargs: Any,
        ) -> Any:
            return await _invoker(_tool, kwargs)

        registered.append(
            router.register(
                _mcp_handler,
                name=qualified_name,
                description=info.description,
                schema=info.schema,
                exposure=exposure,
                metadata={"mcp_server": server, "mcp_tool": original_name},
            )
        )
    return registered


def mcp_invoker(
    client: "McpStdioClient",
) -> Callable[[str, dict[str, Any]], Awaitable[Any]]:
    """Adapt `McpStdioClient.call_tool` to the catalog invoker contract.

    Maps ``isError`` results to failed `ToolResult`s (follow-up allowed —
    the model sees the server's error text and can correct its arguments)
    and text/structured payloads to successful ones.
    """

    async def _invoke(name: str, arguments: dict[str, Any]) -> Any:
        result = await client.call_tool(name, arguments)
        if result.is_error:
            return {"success": False, "error": result.text, "needsFollowup": True}
        data: dict[str, Any] = {"value": result.text}
        if result.structured is not None:
            data["structured"] = result.structured
        return {"success": True, "data": data}

    return _invoke


# ---------------------------------------------------------------------------
# Minimal stdio client (NDJSON JSON-RPC 2.0)
# ---------------------------------------------------------------------------


class McpStdioClient:
    """MCP client over a spawned subprocess's stdin/stdout.

    Speaks newline-delimited JSON-RPC 2.0: ``initialize`` handshake on
    entry, cursor-paginated ``tools/list``, and ``tools/call``. Server
    notifications are logged and ignored; server-initiated *requests*
    (sampling, roots) are answered with a JSON-RPC "method not found"
    error so a server never wedges waiting on a reply.

    One in-flight-request map keyed by monotonically increasing id; every
    request carries ``request_timeout`` (a wedged server fails the call,
    not the loop — the loop's own per-tool timeout is the outer backstop).
    """

    def __init__(
        self,
        command: str,
        args: Sequence[str] = (),
        *,
        env: Mapping[str, str] | None = None,
        request_timeout: float = 30.0,
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
    ) -> None:
        self._command = command
        self._args = tuple(args)
        self._env = dict(env) if env is not None else None
        self._request_timeout = request_timeout
        self._protocol_version = protocol_version
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        #: Server-advertised metadata from the initialize handshake.
        self.server_info: dict[str, Any] = {}

    async def __aenter__(self) -> McpStdioClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def start(self) -> None:
        if self._process is not None:
            raise McpError("McpStdioClient already started")
        env = {**os.environ, **self._env} if self._env is not None else None
        self._process = await asyncio.create_subprocess_exec(
            self._command,
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # Inherit stderr: server diagnostics stay visible to the host.
            stderr=None,
            env=env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        try:
            result = await self._request(
                "initialize",
                {
                    "protocolVersion": self._protocol_version,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "steerable-agent-runtime",
                        "version": "0.1.0",
                    },
                },
            )
        except Exception:
            await self.aclose()
            raise
        self.server_info = dict(result.get("serverInfo") or {})
        await self._notify("notifications/initialized")

    async def aclose(self) -> None:
        process, self._process = self._process, None
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        for pending in self._pending.values():
            if not pending.done():
                pending.set_exception(McpError("MCP client closed"))
        self._pending.clear()
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:  # builtin TimeoutError pre-3.11 alias
                process.kill()
                await process.wait()

    async def list_tools(self) -> list[McpToolInfo]:
        """Full catalog, following ``nextCursor`` pagination."""
        tools: list[McpToolInfo] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {}
            if cursor is not None:
                params["cursor"] = cursor
            result = await self._request("tools/list", params)
            for entry in result.get("tools") or []:
                tools.append(
                    McpToolInfo(
                        name=str(entry.get("name") or ""),
                        description=str(entry.get("description") or ""),
                        schema=dict(
                            entry.get("inputSchema")
                            or {"type": "object", "properties": {}}
                        ),
                    )
                )
            cursor = result.get("nextCursor") or None
            if cursor is None:
                return tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> McpCallResult:
        result = await self._request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        parts: list[str] = []
        for content in result.get("content") or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "text":
                parts.append(str(content.get("text") or ""))
            else:
                parts.append(f"[mcp {content.get('type', 'unknown')} content]")
        return McpCallResult(
            text="\n".join(parts),
            is_error=bool(result.get("isError")),
            structured=result.get("structuredContent"),
        )

    # ------------------------------------------------------------------
    # Wire plumbing
    # ------------------------------------------------------------------

    async def _request(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        process = self._require_process()
        self._next_id += 1
        request_id = self._next_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            return await asyncio.wait_for(future, timeout=self._request_timeout)
        except asyncio.TimeoutError as exc:
            # asyncio.TimeoutError only became an alias of builtin
            # TimeoutError in 3.11; on 3.10 they are distinct classes.
            raise McpError(
                f"MCP request {method!r} timed out after {self._request_timeout}s"
            ) from exc
        finally:
            self._pending.pop(request_id, None)

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._require_process()
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None or self._process.returncode is not None:
            raise McpError(
                f"MCP server {self._command!r} is not running "
                f"(exit {self._process.returncode if self._process else 'never started'})"
            )
        return self._process

    async def _write(self, message: dict[str, Any]) -> None:
        process = self._require_process()
        assert process.stdin is not None
        process.stdin.write(json.dumps(message).encode() + b"\n")
        # Large tool arguments can exceed the pipe buffer; drain so a big
        # tools/call can't silently truncate.
        await process.stdin.drain()

    async def _read_loop(self) -> None:
        process = self._require_process()
        assert process.stdout is not None
        try:
            async for line in process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("MCP server sent non-JSON line: %r", line[:200])
                    continue
                if "id" in message and ("result" in message or "error" in message):
                    future = self._pending.get(message["id"])
                    if future is None or future.done():
                        continue  # late response to a timed-out request
                    if "error" in message:
                        error = message["error"] or {}
                        future.set_exception(
                            McpError(
                                f"MCP error {error.get('code')}: "
                                f"{error.get('message')}"
                            )
                        )
                    else:
                        future.set_result(message.get("result") or {})
                elif "id" in message and "method" in message:
                    # Server-initiated request (sampling, roots, …): answer
                    # method-not-found so the server never wedges on us.
                    await self._write(
                        {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "error": {
                                "code": -32601,
                                "message": f"Unsupported server request: {message['method']}",
                            },
                        }
                    )
                else:
                    logger.debug("MCP notification ignored: %s", message.get("method"))
        except asyncio.CancelledError:
            raise
        finally:
            # EOF or reader teardown: fail every in-flight request.
            for pending in self._pending.values():
                if not pending.done():
                    pending.set_exception(McpError("MCP server closed the stream"))
