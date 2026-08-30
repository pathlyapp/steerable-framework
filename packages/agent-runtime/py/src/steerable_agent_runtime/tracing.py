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

import hashlib
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from steerable_agent_protocol.generated import HarnessTrace, TraceEvent, TraceSpan
from steerable_agent_harness.tracing import sanitize_for_trace

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
    """Records a CoreLoop run into storage as trace + spans + events.

    Span model (W2.7.2, OTel semantics): the run is the root; each provider
    request is an ``llm`` span (one per attempt — retries are visible), each
    tool dispatch a ``tool`` span, and each interactive approval wait an
    ``approval`` span parented to its tool span. Events stay point-in-time
    annotations.

    ``sample_rate`` is head-based sampling: the decision is made once per
    trace from the trace id's hash (deterministic — re-recording the same
    trace id lands in the same bucket), and unsampled traces pass events
    through ``tee`` untouched without persisting anything.
    """

    def __init__(
        self,
        storage: StorageAdapter,
        *,
        trace_id: str | None = None,
        chat_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        max_payload_chars: int = 500,
        sample_rate: float = 1.0,
    ) -> None:
        if not 0.0 <= sample_rate <= 1.0:
            raise ValueError(f"sample_rate must be in [0, 1], got {sample_rate}")
        self._storage = storage
        self.trace_id = trace_id or f"trace_{uuid.uuid4().hex}"
        self._chat_id = chat_id
        self._session_id = session_id
        self._user_id = user_id
        self._max_payload = max_payload_chars
        # Deterministic head sampling: the same trace id always lands in the
        # same bucket, so re-recording a run (resume, fork) is consistent.
        bucket = int(hashlib.sha256(self.trace_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        self._sampled = bucket <= sample_rate if sample_rate < 1.0 else True

        self._sequence = 0
        self._span_count = 0
        self._open_spans: dict[str, dict[str, Any]] = {}
        self._open_llm: dict[tuple[int, int], dict[str, Any]] = {}
        self._started_ms = _now_ms()
        self._had_error = False
        self._final_status: str | None = None
        self._created_iso: str | None = None

    async def tee(self, events: AsyncIterator[LoopEvent]) -> AsyncIterator[LoopEvent]:
        """Pass-through wrapper that records each event as it flows."""
        async for event in events:
            await self.record(event)
            yield event
        await self.finalize()

    async def record(self, event: LoopEvent) -> None:
        if not self._sampled:
            return
        if self._sequence == 0:
            # Write the trace row up front with status="running": events and
            # spans already persist incrementally, but without the row a
            # mid-turn trace.fetch reports "trace not found" — live turns
            # were uninspectable. finalize() overwrites with the terminal
            # status.
            await self._upsert("running")
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
                    # Secret-redact before truncating/persisting (spec
                    # "Secret redaction") — a tool result echoing a key must
                    # never land in the trace.
                    payload=_truncate(
                        sanitize_for_trace(dict(event.data)), self._max_payload
                    ),
                )
            ],
        )

        if event.kind == "tool_call_start":
            self._open_spans[event.data["id"]] = {
                "name": event.data["name"],
                "startMs": _now_ms(),
            }
        elif event.kind == "llm_request":
            self._open_llm[(event.data["round"], event.data["attempt"])] = {
                "startMs": _now_ms(),
            }
        elif event.kind == "llm_response":
            opened = self._open_llm.pop(
                (event.data["round"], event.data["attempt"]), None
            )
            if opened is not None:
                self._span_count += 1
                error = event.data.get("error")
                await self._storage.append_spans(
                    self.trace_id,
                    [
                        TraceSpan(
                            spanId=f"span_{self._span_count:04d}",
                            traceId=self.trace_id,
                            name="llm.request",
                            kind="llm",
                            startMs=opened["startMs"],
                            endMs=_now_ms(),
                            durationMs=event.data.get("durationMs"),
                            status="error" if error else "ok",
                            attrs=sanitize_for_trace(
                                {
                                    "round": event.data["round"],
                                    "attempt": event.data["attempt"],
                                    "promptTokens": event.data.get("promptTokens"),
                                    "cachedPromptTokens": event.data.get(
                                        "cachedPromptTokens"
                                    ),
                                    **(
                                        {"error": str(error)[: self._max_payload]}
                                        if error
                                        else {}
                                    ),
                                }
                            ),
                        )
                    ],
                )
                if error:
                    self._had_error = True
        elif event.kind in ("tool_call_result", "tool_error"):
            opened = self._open_spans.pop(event.data["id"], None)
            if opened is not None:
                self._span_count += 1
                success = bool(event.data.get("success", False))
                tool_span_id = f"span_{self._span_count:04d}"
                spans = [
                    TraceSpan(
                        spanId=tool_span_id,
                        traceId=self.trace_id,
                        name=opened["name"],
                        kind="tool",
                        startMs=opened["startMs"],
                        endMs=_now_ms(),
                        durationMs=event.data.get("durationMs"),
                        status="ok" if success else "error",
                        attrs=sanitize_for_trace(
                            {
                                "toolCallId": event.data["id"],
                                **(
                                    {"error": str(event.data["error"])[: self._max_payload]}
                                    if "error" in event.data
                                    else {}
                                ),
                            }
                        ),
                    )
                ]
                # W2.7.2: an interactive approval wait inside the dispatch
                # becomes its own span, parented to the tool span — approval
                # latency is attributable instead of hiding in tool time.
                approval = event.data.get("approval")
                if isinstance(approval, dict) and approval.get("waitMs") is not None:
                    self._span_count += 1
                    wait_ms = int(approval["waitMs"])
                    spans.append(
                        TraceSpan(
                            spanId=f"span_{self._span_count:04d}",
                            traceId=self.trace_id,
                            parentSpanId=tool_span_id,
                            name="approval.wait",
                            kind="approval",
                            startMs=opened["startMs"],
                            endMs=opened["startMs"] + wait_ms,
                            durationMs=wait_ms,
                            status="ok",
                            attrs=sanitize_for_trace(
                                {
                                    "kind": approval.get("kind"),
                                    "category": approval.get("category"),
                                }
                            ),
                        )
                    )
                await self._storage.append_spans(self.trace_id, spans)
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
        manually if you consume events via ``record`` directly. Idempotent —
        safe to call again from a finally guard after an abnormal exit."""

        if getattr(self, "_finalized", False):
            if not self._sampled:
                return self._build_trace(status or self._final_status or "failed")
            return await self._storage.get_trace(self.trace_id)  # type: ignore[return-value]
        self._finalized = True
        if not self._sampled:
            # Unsampled: nothing was persisted; return the summary object
            # without touching storage.
            return self._build_trace(status or self._final_status or "failed")
        return await self._upsert(status or self._final_status or "failed")

    def _build_trace(self, status: str) -> HarnessTrace:
        now_iso = datetime.now(timezone.utc).isoformat()
        self._created_iso = self._created_iso or now_iso
        return HarnessTrace(
            traceId=self.trace_id,
            userId=self._user_id,
            chatId=self._chat_id,
            sessionId=self._session_id,
            status=status,
            durationMs=_now_ms() - self._started_ms,
            hadError=self._had_error,
            eventCount=self._sequence,
            spanCount=self._span_count,
            createdAt=self._created_iso,
            updatedAt=now_iso,
        )

    async def _upsert(self, status: str) -> HarnessTrace:
        return await self._storage.upsert_trace(self._build_trace(status))
