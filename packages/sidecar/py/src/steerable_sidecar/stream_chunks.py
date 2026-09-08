"""Pre-digestion stream-chunk consumers (the ``on_stream_chunk`` hook).

The CoreLoop's ``on_stream_chunk`` observation hook fires on every raw
``LLMStreamChunk`` *before* the loop digests it — before UI-tag stripping
(``PseudoStreamStripper``) and surrogate splitting turn content into display
text. The digested ``stream.chunk`` fields (``delta`` / ``reasoningDelta``)
are post-stripping display text; hosts running incremental renderers (e.g. a
streaming UI-tag parser) need the pre-digestion stream, forwarded here under
a separate ``rawChunk`` field so the two never conflate.

Provider reality: the default OpenAI-compat provider buffers tool-call
argument fragments into one complete ``ToolCall``, so ``toolCallDelta``
arrives whole — no incremental argument fragments. ``contentDelta`` /
``reasoningDelta`` are genuinely incremental.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from steerable_agent_runtime import LoopContext, NoopHooks
from steerable_agent_runtime.llm import LLMStreamChunk
from steerable_agent_runtime.transport.stdio_jsonrpc import StdioJsonRpcTransport

logger = logging.getLogger(__name__)


def serialize_raw_chunk(chunk: LLMStreamChunk) -> dict[str, Any]:
    """Project one ``LLMStreamChunk`` onto its wire dict (unset fields omitted).

    ``raw`` (the provider's original wire chunk) is deliberately excluded:
    it can be huge and carries wire-level detail no host should depend on.
    ``usage`` / ``reasoning_details`` are loop telemetry, not renderer input
    — the turn's billable usage already rides ``stream.done``.
    """
    payload: dict[str, Any] = {}
    if chunk.content_delta is not None:
        payload["contentDelta"] = chunk.content_delta
    if chunk.reasoning_delta is not None:
        payload["reasoningDelta"] = chunk.reasoning_delta
    if chunk.tool_call_delta is not None:
        call = chunk.tool_call_delta
        payload["toolCallDelta"] = {
            "id": call.id,
            "name": call.name,
            "arguments": call.arguments,
        }
    if chunk.finish_reason is not None:
        payload["finishReason"] = chunk.finish_reason
    return payload


def _log_emit_failure(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("raw chunk emit failed: %s", exc)


class RawChunkBridgeHooks(NoopHooks):
    """Forward every pre-digestion chunk as a ``stream.chunk`` notification.

    The chunk rides under ``rawChunk`` next to ``streamId``, distinct from
    the digested ``delta`` / ``reasoningDelta`` fields the loop's own events
    produce. Emission is fire-and-forget (``asyncio.ensure_future``) because
    the hook is synchronous while the transport is async — the same pattern
    as ``Sidecar._emit_child_event``. Chunk order is preserved (tasks run in
    scheduling order); ordering against the digested notifications is not,
    so a consumer keys on one field family or the other.
    """

    def __init__(self, transport: StdioJsonRpcTransport, stream_id: str) -> None:
        self._transport = transport
        self._stream_id = stream_id

    def on_stream_chunk(self, chunk: Any, ctx: LoopContext) -> None:
        try:
            payload = serialize_raw_chunk(chunk)
            if not payload:
                return
            task = asyncio.ensure_future(
                self._transport.emit_notification(
                    "stream.chunk",
                    {"streamId": self._stream_id, "rawChunk": payload},
                )
            )
            task.add_done_callback(_log_emit_failure)
        except Exception:  # noqa: BLE001 — observation must not break the loop
            logger.exception("raw_chunk_bridge_failed")


class RawChunkStdoutHooks(NoopHooks):
    """Headless debug: write each pre-digestion chunk as a tagged stdout line.

    Gated by ``STEERABLE_RAW_CHUNKS`` in ``headless._run``. The
    ``[raw_chunk {...}]`` line format matches the other bracketed headless
    markers (``[tool ...]``, ``[hook_action ...]``) so log parsers can skip
    it; the digested content stream is unaffected.
    """

    def on_stream_chunk(self, chunk: Any, ctx: LoopContext) -> None:
        try:
            payload = serialize_raw_chunk(chunk)
            if payload:
                sys.stdout.write(
                    f"\n[raw_chunk {json.dumps(payload, ensure_ascii=False)}]\n"
                )
                sys.stdout.flush()
        except Exception:  # noqa: BLE001 — observation must not break the loop
            logger.exception("raw_chunk_stdout_failed")
