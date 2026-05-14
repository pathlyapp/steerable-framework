"""Server-Sent Events transport built on top of FastAPI / Starlette."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from steerable_agent_protocol.generated import SSEEvent

from ..errors import TransportError


class FastAPISseTransport:
    """Per-request SSE transport.

    Usage::

        @app.post("/chat/run")
        async def run_chat(...):
            transport = FastAPISseTransport()
            asyncio.create_task(_run_loop(transport, ...))
            return await sse_response(transport)
    """

    def __init__(self, *, queue_size: int = 256) -> None:
        self._queue: asyncio.Queue[SSEEvent | None] = asyncio.Queue(maxsize=queue_size)
        self._closed = False

    async def emit(self, event: SSEEvent) -> None:
        if self._closed:
            raise TransportError("Transport is already closed")
        await self._queue.put(event)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)

    async def stream(self) -> AsyncIterator[SSEEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event


async def sse_response(
    transport: FastAPISseTransport,
    *,
    media_type: str = "text/event-stream",
    headers: dict[str, str] | None = None,
):
    """Build a Starlette ``StreamingResponse`` that drains ``transport``.

    The response will keep the connection open until ``transport.aclose()`` is
    called from the producing task.
    """

    try:
        from starlette.responses import StreamingResponse  # local import (optional dep)
    except Exception as exc:  # pragma: no cover
        raise ImportError(
            "FastAPISseTransport.stream requires `starlette` (install "
            "`steerable-agent-runtime[fastapi]`)"
        ) from exc

    async def _gen() -> AsyncIterator[bytes]:
        async for event in transport.stream():
            payload = encode_sse_event(event)
            yield payload.encode("utf-8")

    response_headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    if headers:
        response_headers.update(headers)
    return StreamingResponse(_gen(), media_type=media_type, headers=response_headers)


def encode_sse_event(event: SSEEvent) -> str:
    """Serialize an `SSEEvent` to a wire-format `data: <json>\n\n` block."""

    body = event.model_dump(exclude_none=True)
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    lines = []
    if event.event:
        lines.append(f"event: {event.event}")
    lines.append(f"data: {payload}")
    return "\n".join(lines) + "\n\n"


def decode_sse_event(raw: str) -> SSEEvent | None:
    """Parse a `data: <json>` block back into `SSEEvent`. Returns None when the
    block is empty or malformed (mirrors browser EventSource behavior)."""

    data: str | None = None
    event: str | None = None
    for line in raw.splitlines():
        if line.startswith("data:"):
            data = (data or "") + line[5:].lstrip()
        elif line.startswith("event:"):
            event = line[6:].strip()
    if data is None:
        return None
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return None
    if event and "event" not in payload:
        payload["event"] = event
    return SSEEvent.model_validate(payload)
