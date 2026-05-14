"""JSON-RPC 2.0 transport over stdio.

Powers the ``steerable-sidecar`` (a portable Python process spawned by an
Electron / desktop host). Each frame is a single-line UTF-8 JSON document
terminated by ``\n``, matching ``spec/sidecar/``.

The module ships two layers:

* ``StdioJsonRpcTransport`` — implements ``TransportAdapter.emit()`` so the
  runtime can stream `SSEEvent` instances over JSON-RPC notifications.
* ``JsonRpcServer`` — minimal request/response/notification dispatcher used by
  the sidecar entrypoint. Handlers are registered with
  ``server.register("method.name", handler)``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from steerable_agent_protocol.generated import (
    SidecarError,
    SidecarNotification,
    SidecarRequest,
    SidecarResponse,
    SSEEvent,
)

logger = logging.getLogger(__name__)

JsonRpcMethodHandler = Callable[[dict[str, Any] | None], Awaitable[Any]]


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


def encode_frame(payload: dict[str, Any]) -> bytes:
    """Encode one JSON-RPC frame to a single-line UTF-8 bytes buffer."""

    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode_frame(line: str) -> dict[str, Any] | None:
    """Decode a single JSON-RPC frame line. Returns None for blank lines."""

    line = line.strip()
    if not line:
        return None
    return json.loads(line)


def build_request(
    *,
    request_id: str | int,
    method: str,
    params: dict[str, Any] | None = None,
) -> SidecarRequest:
    return SidecarRequest(jsonrpc="2.0", id=request_id, method=method, params=params)


def build_notification(
    *,
    method: str,
    params: dict[str, Any] | None = None,
) -> SidecarNotification:
    return SidecarNotification(jsonrpc="2.0", method=method, params=params)


def build_response(
    *,
    request_id: str | int | None,
    result: Any | None = None,
    error: SidecarError | None = None,
) -> SidecarResponse:
    return SidecarResponse(jsonrpc="2.0", id=request_id, result=result, error=error)


def make_error(
    *,
    code: int,
    message: str,
    kind: str | None = None,
    data: Any | None = None,
) -> SidecarError:
    return SidecarError(code=code, message=message, kind=kind, data=data)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _PendingRequest:
    future: asyncio.Future


class StdioJsonRpcTransport:
    """Runtime-side adapter that emits `SSEEvent` as JSON-RPC notifications.

    Concrete writer is injected so this class is testable without touching real
    stdio: pass any object supporting ``write(bytes) -> Awaitable[None]`` (or a
    sync ``write(bytes) -> int``).
    """

    def __init__(
        self,
        writer: Any,
        *,
        notification_method: str = "stream.chunk",
    ) -> None:
        self._writer = writer
        self._notification_method = notification_method
        self._closed = False
        self._lock = asyncio.Lock()

    async def emit(self, event: SSEEvent) -> None:
        if self._closed:
            raise RuntimeError("Transport is closed")
        notification = build_notification(
            method=self._notification_method,
            params=event.model_dump(exclude_none=True),
        )
        await self._send(notification.model_dump(exclude_none=True))

    async def emit_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._closed:
            raise RuntimeError("Transport is closed")
        notification = build_notification(method=method, params=params)
        await self._send(notification.model_dump(exclude_none=True))

    async def aclose(self) -> None:
        self._closed = True

    async def _send(self, payload: dict[str, Any]) -> None:
        frame = encode_frame(payload)
        async with self._lock:
            result = self._writer.write(frame)
            if asyncio.iscoroutine(result):
                await result
            drain = getattr(self._writer, "drain", None)
            if drain is not None:
                drained = drain()
                if asyncio.iscoroutine(drained):
                    await drained


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class JsonRpcServer:
    """Minimal stdio JSON-RPC dispatcher.

    Handlers are async callables ``handler(params: dict | None) -> Any``.

    Returning ``None`` from a handler still produces a JSON-RPC ``result: null``
    response (so the client knows the request succeeded). Raise to surface an
    error; ``JsonRpcError`` instances pass their ``code/kind/data`` through.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, JsonRpcMethodHandler] = {}
        self._notification_handlers: dict[str, JsonRpcMethodHandler] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, method: str, handler: JsonRpcMethodHandler) -> None:
        if method in self._handlers:
            raise ValueError(f"Method already registered: {method}")
        self._handlers[method] = handler

    def register_notification(self, method: str, handler: JsonRpcMethodHandler) -> None:
        if method in self._notification_handlers:
            raise ValueError(f"Notification handler already registered: {method}")
        self._notification_handlers[method] = handler

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def handle_frame(self, raw: str) -> dict[str, Any] | None:
        try:
            payload = decode_frame(raw)
        except json.JSONDecodeError as exc:
            return build_response(
                request_id=None,
                error=make_error(code=-32700, kind="parse", message=str(exc)),
            ).model_dump(exclude_none=True)
        if payload is None:
            return None
        if "id" not in payload:  # notification
            await self._dispatch_notification(payload)
            return None
        return await self._dispatch_request(payload)

    async def _dispatch_notification(self, payload: dict[str, Any]) -> None:
        method = payload.get("method")
        handler = self._notification_handlers.get(method or "")
        if handler is None:
            logger.debug("Unhandled notification: %s", method)
            return
        try:
            await handler(payload.get("params"))
        except Exception:  # noqa: BLE001
            logger.exception("Notification handler %s raised", method)

    async def _dispatch_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("id")
        method = payload.get("method")
        if not isinstance(method, str) or not method:
            return build_response(
                request_id=request_id,
                error=make_error(code=-32600, kind="invalid_request", message="missing method"),
            ).model_dump(exclude_none=True)
        handler = self._handlers.get(method)
        if handler is None:
            return build_response(
                request_id=request_id,
                error=make_error(
                    code=-32601, kind="method_not_found", message=f"unknown method '{method}'"
                ),
            ).model_dump(exclude_none=True)
        params = payload.get("params")
        try:
            result = await handler(params)
        except JsonRpcError as exc:
            return build_response(
                request_id=request_id,
                error=make_error(code=exc.code, kind=exc.kind, message=exc.message, data=exc.data),
            ).model_dump(exclude_none=True)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Method %s raised", method)
            return build_response(
                request_id=request_id,
                error=make_error(code=-32603, kind="internal", message=str(exc)),
            ).model_dump(exclude_none=True)
        return build_response(request_id=request_id, result=result).model_dump(exclude_none=True)

    # ------------------------------------------------------------------
    # Pump
    # ------------------------------------------------------------------

    async def serve_stdio(
        self,
        *,
        reader: Any | None = None,
        writer: Any | None = None,
    ) -> None:
        """Drive the server using asyncio stdio streams.

        When ``reader`` / ``writer`` are not provided, fall back to wrapping
        ``sys.stdin`` and ``sys.stdout`` so the sidecar entrypoint can simply
        call ``await server.serve_stdio()``.
        """

        if reader is None or writer is None:
            reader, writer = await _connect_default_stdio()
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                response = await self.handle_frame(line.decode("utf-8"))
                if response is None:
                    continue
                writer.write(encode_frame(response))
                drain = getattr(writer, "drain", None)
                if drain is not None:
                    drained = drain()
                    if asyncio.iscoroutine(drained):
                        await drained
        finally:
            close = getattr(writer, "close", None)
            if close is not None:
                close()


class JsonRpcError(Exception):
    """Raise from a handler to return a structured JSON-RPC error."""

    def __init__(
        self,
        message: str,
        *,
        code: int = -32000,
        kind: str | None = None,
        data: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind
        self.data = data
        self.message = message


# ---------------------------------------------------------------------------
# Default stdio adapter
# ---------------------------------------------------------------------------


async def _connect_default_stdio() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    transport, _ = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout)
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    return reader, writer
