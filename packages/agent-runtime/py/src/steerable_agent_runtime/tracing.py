"""Trace recording — tee the loop's event stream into a StorageAdapter.

The loop stays storage-free (events are the single source of truth); this
recorder subscribes to the same stream and persists it as a `HarnessTrace`
with spans (one per tool call) and events (one per loop event). That is the
observability primitive codex gets from rollout/trace bundles — here it is
just a consumer, so it works with any storage backend and never changes loop
behavior.

Usage::

    recorder = TraceRecorder(storage, chat_id="chat_1")
    async for event in recorder.tee(loop.run(messages)):
        ...  # emit to the user as usual
    # on stream end the trace is finalized automatically

Payloads are string-truncated before persisting (tool results can be huge —
spilled or not, traces should stay small).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from steerable_agent_protocol.generated import HarnessTrace, TraceEvent, TraceSpan

from .loop import LoopEvent
from .storage import StorageAdapter

_TERMINAL_STATUSES = {"completed", "failed", "budget_exhausted"}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _truncate(value: Any, max_chars: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + "…"
    if isinstance(value, dict):
        return {k: _truncate(v, max_chars) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v, max_chars) for v in value[:50]]
    return value


class TraceRecorder:
    """Records a CoreLoop run into storage as trace + spans + events."""

    def __init__(
        self,
        storage: StorageAdapter,
        *,
        trace_id: str | None = None,
        chat_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        max_payload_chars: int = 500,
    ) -> None:
        self._storage = storage
        self.trace_id = trace_id or f"trace_{uuid.uuid4().hex}"
        self._chat_id = chat_id
        self._session_id = session_id
        self._user_id = user_id
        self._max_payload = max_payload_chars

        self._sequence = 0
        self._span_count = 0
        self._open_spans: dict[str, dict[str, Any]] = {}
        self._started_ms = _now_ms()
        self._had_error = False
        self._final_status: str | None = None

    async def tee(self, events: AsyncIterator[LoopEvent]) -> AsyncIterator[LoopEvent]:
        """Pass-through wrapper that records each event as it flows."""
        async for event in events:
            await self.record(event)
            yield event
        await self.finalize()

    async def record(self, event: LoopEvent) -> None:
        self._sequence += 1
        await self._storage.append_events(
            self.trace_id,
            [
                TraceEvent(
                    traceId=self.trace_id,
                    kind=event.kind,
                    name=str(event.data.get("name") or event.kind),
                    sequence=self._sequence,
                    timestampMs=_now_ms(),
                    status=event.data.get("status"),
                    payload=_truncate(dict(event.data), self._max_payload),
                )
            ],
        )

        if event.kind == "tool_call_start":
            self._open_spans[event.data["id"]] = {
                "name": event.data["name"],
                "startMs": _now_ms(),
            }
        elif event.kind in ("tool_call_result", "tool_error"):
            opened = self._open_spans.pop(event.data["id"], None)
            if opened is not None:
                self._span_count += 1
                success = bool(event.data.get("success", False))
                await self._storage.append_spans(
                    self.trace_id,
                    [
                        TraceSpan(
                            spanId=f"span_{self._span_count:04d}",
                            traceId=self.trace_id,
                            name=opened["name"],
                            kind="tool",
                            startMs=opened["startMs"],
                            endMs=_now_ms(),
                            durationMs=event.data.get("durationMs"),
                            status="ok" if success else "error",
                            attrs={
                                "toolCallId": event.data["id"],
                                **(
                                    {"error": str(event.data["error"])[: self._max_payload]}
                                    if "error" in event.data
                                    else {}
                                ),
                            },
                        )
                    ],
                )
                if not success:
                    self._had_error = True
        elif event.kind == "error":
            self._had_error = True
        elif event.kind == "completion" and event.data.get("status") in _TERMINAL_STATUSES:
            self._final_status = event.data["status"]
            if self._final_status == "failed":
                self._had_error = True

    async def finalize(self, *, status: str | None = None) -> HarnessTrace:
        """Upsert the trace summary. Called by ``tee`` at stream end; call
        manually if you consume events via ``record`` directly."""

        final = status or self._final_status or "failed"
        now_iso = datetime.now(timezone.utc).isoformat()
        trace = HarnessTrace(
            traceId=self.trace_id,
            userId=self._user_id,
            chatId=self._chat_id,
            sessionId=self._session_id,
            status=final,
            durationMs=_now_ms() - self._started_ms,
            hadError=self._had_error,
            eventCount=self._sequence,
            spanCount=self._span_count,
            createdAt=now_iso,
            updatedAt=now_iso,
        )
        return await self._storage.upsert_trace(trace)
