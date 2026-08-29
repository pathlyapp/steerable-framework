"""AG-UI transport: render CoreLoop events in the AG-UI wire format.

The sidecar's bespoke ``stream.chunk`` notifications stay for DeepPath
byte-compatibility; this renderer is the peer that proves the loop's event
taxonomy is transport-neutral (the roadmap's protocol-positioning call:
transports render wire formats; the taxonomy does not bend to them).

Mapping (LoopEventKind → AG-UI):

- ``content_delta``   → TEXT_MESSAGE_START (first delta of a message) +
  TEXT_MESSAGE_CONTENT; a tool call or reasoning delta closes the open
  message first (AG-UI messages don't interleave with tool calls).
- ``reasoning_delta`` → REASONING_MESSAGE_START + REASONING_MESSAGE_CONTENT.
- ``tool_call_start`` → TOOL_CALL_START + TOOL_CALL_ARGS (the loop carries
  full arguments, so one args event) + TOOL_CALL_END.
- ``tool_call_result`` / ``tool_error`` → TOOL_CALL_RESULT (errors ride in
  the result content — AG-UI has no tool-call-error event).
- ``completion``      → closes any open message, then RUN_FINISHED
  (``completed`` / ``budget_exhausted``) or RUN_ERROR (``failed``).
- ``stage_start`` / ``stage_complete`` / ``hook_action`` / ``steer`` /
  ``soft_timeout`` / ``budget_exhausted`` / ``error`` → CUSTOM events named
  ``steerable.<kind>`` — lossless, and honest that these are framework
  observability events, not AG-UI "steps".

Serving is the embedder's job (AG-UI canonically runs SSE over HTTP, and
the embedder's web tier owns HTTP); ``encode_sse`` renders the byte stream.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ag_ui.core import (
    BaseEvent,
    CustomEvent,
    EventType,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)

if TYPE_CHECKING:
    from steerable_agent_runtime import LoopEvent

__all__ = ["AgUiRenderer", "encode_sse"]


class AgUiRenderer:
    """Stateful per-run projection of ``LoopEvent`` into AG-UI events.

    One renderer per run: message/reasoning boundaries are tracked so text
    and reasoning streams open and close exactly once per segment.
    """

    def __init__(self, thread_id: str, run_id: str) -> None:
        self._thread_id = thread_id
        self._run_id = run_id
        self._seq = 0
        self._open_message: str | None = None
        self._open_reasoning: str | None = None

    def begin(self) -> list[BaseEvent]:
        """The RUN_STARTED event that opens every AG-UI run."""
        return [RunStartedEvent(thread_id=self._thread_id, run_id=self._run_id)]

    def render(self, event: LoopEvent) -> list[BaseEvent]:
        kind = event.kind
        data = event.data
        if kind == "content_delta":
            return self._content(data.get("delta", ""))
        if kind == "reasoning_delta":
            return self._reasoning(data.get("delta", ""))
        if kind == "tool_call_start":
            return self._tool_call_start(data)
        if kind in ("tool_call_result", "tool_error"):
            return self._tool_call_result(data)
        if kind == "completion":
            return self._completion(data)
        if kind == "error":
            return [
                *self._close_all(),
                RunErrorEvent(message=str(data.get("message") or data)),
            ]
        # Framework observability events travel as CUSTOM, lossless.
        return [CustomEvent(name=f"steerable.{kind}", value=dict(data))]

    # ------------------------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{self._run_id}-{prefix}-{self._seq}"

    def _close_all(self) -> list[BaseEvent]:
        out: list[BaseEvent] = []
        if self._open_reasoning is not None:
            out.append(ReasoningMessageEndEvent(message_id=self._open_reasoning))
            self._open_reasoning = None
        if self._open_message is not None:
            out.append(TextMessageEndEvent(message_id=self._open_message))
            self._open_message = None
        return out

    def _content(self, delta: str) -> list[BaseEvent]:
        out: list[BaseEvent] = []
        if self._open_message is None:
            out.extend(self._close_all())
            self._open_message = self._next_id("msg")
            out.append(TextMessageStartEvent(message_id=self._open_message))
        out.append(
            TextMessageContentEvent(message_id=self._open_message, delta=delta)
        )
        return out

    def _reasoning(self, delta: str) -> list[BaseEvent]:
        out: list[BaseEvent] = []
        if self._open_reasoning is None:
            out.extend(self._close_all())
            self._open_reasoning = self._next_id("think")
            out.append(
                ReasoningMessageStartEvent(
                    message_id=self._open_reasoning, role="reasoning"
                )
            )
        out.append(
            ReasoningMessageContentEvent(
                message_id=self._open_reasoning, delta=delta
            )
        )
        return out

    def _tool_call_start(self, data: dict[str, Any]) -> list[BaseEvent]:
        call_id = str(data.get("id", ""))
        out = self._close_all()
        out.append(
            ToolCallStartEvent(
                tool_call_id=call_id,
                tool_call_name=str(data.get("name", "")),
            )
        )
        # The loop carries complete arguments (no streaming arg deltas), so
        # the args payload is a single event and the call closes at once.
        out.append(
            ToolCallArgsEvent(
                tool_call_id=call_id,
                delta=json.dumps(data.get("arguments") or {}, ensure_ascii=False),
            )
        )
        out.append(ToolCallEndEvent(tool_call_id=call_id))
        return out

    def _tool_call_result(self, data: dict[str, Any]) -> list[BaseEvent]:
        call_id = str(data.get("id", ""))
        if data.get("success", False):
            content = str(data.get("result") or data.get("resultPreview") or "")
        else:
            content = json.dumps(
                {"success": False, "error": data.get("error", "tool failed")},
                ensure_ascii=False,
            )
        return [
            ToolCallResultEvent(
                message_id=self._next_id("result"),
                tool_call_id=call_id,
                content=content,
            )
        ]

    def _completion(self, data: dict[str, Any]) -> list[BaseEvent]:
        out = self._close_all()
        status = data.get("status")
        if status == "failed":
            out.append(
                RunErrorEvent(message=str(data.get("reason") or "run failed"))
            )
        else:
            out.append(
                RunFinishedEvent(thread_id=self._thread_id, run_id=self._run_id)
            )
        return out


def encode_sse(events: list[BaseEvent]) -> str:
    """Encode events as an SSE byte stream (AG-UI's canonical transport).

    The embedder's web tier owns HTTP; this renders the wire bytes.
    """
    from ag_ui.encoder import EventEncoder

    encoder = EventEncoder()
    return "".join(encoder.encode(event) for event in events)


#: The event types this renderer can emit — for conformance checks in tests
#: and for embedders negotiating capabilities.
EMITTED_EVENT_TYPES = frozenset(
    {
        EventType.RUN_STARTED,
        EventType.RUN_FINISHED,
        EventType.RUN_ERROR,
        EventType.TEXT_MESSAGE_START,
        EventType.TEXT_MESSAGE_CONTENT,
        EventType.TEXT_MESSAGE_END,
        EventType.REASONING_MESSAGE_START,
        EventType.REASONING_MESSAGE_CONTENT,
        EventType.REASONING_MESSAGE_END,
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_ARGS,
        EventType.TOOL_CALL_END,
        EventType.TOOL_CALL_RESULT,
        EventType.CUSTOM,
    }
)
