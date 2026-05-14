"""TransportAdapter interface + reference implementations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from steerable_agent_protocol.generated import SSEEvent


@runtime_checkable
class TransportAdapter(Protocol):
    """Abstract bidirectional transport.

    The runtime emits `SSEEvent` instances via `emit()` and receives requests
    via the implementation-specific entrypoint (HTTP route handler, stdio
    pump, websocket message, etc.).
    """

    async def emit(self, event: SSEEvent) -> None: ...

    async def aclose(self) -> None: ...


from .fastapi_sse import FastAPISseTransport, sse_response  # noqa: E402
from .stdio_jsonrpc import (  # noqa: E402
    StdioJsonRpcTransport,
    JsonRpcMethodHandler,
    JsonRpcServer,
)

__all__ = [
    "TransportAdapter",
    "FastAPISseTransport",
    "sse_response",
    "StdioJsonRpcTransport",
    "JsonRpcMethodHandler",
    "JsonRpcServer",
]
