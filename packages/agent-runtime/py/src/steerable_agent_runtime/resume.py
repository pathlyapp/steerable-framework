"""Session resume — project a persisted trace back into an LLM transcript.

The loop is storage-free and the trace event stream is the durable record;
this module is the inverse projection: given the recorded events of one run
(in sequence order), rebuild the ``list[LLMMessage]`` the loop had
constructed, so a follow-up turn can seed a new ``CoreLoop.run(messages)``
without re-asking the user or re-running tools.

Fidelity:
- assistant text comes from ``content_delta`` events (display-cleaned —
  hidden tool-call markup is already stripped, which is what we want to
  resume from);
- tool calls come from ``tool_call_start`` (full arguments);
- tool results come from ``tool_call_result``: the full ``result`` field
  when the loop ran with ``LoopConfig.persist_tool_results=True`` (and the
  recorder's ``max_payload_chars`` was large enough not to truncate it),
  otherwise the 300-char ``resultPreview`` — lossy but preserves the gist
  the model already acted on.

Bookkeeping events (stage/stage_complete/soft_timeout/budget/completion,
discipline-retry scaffolding) are skipped: the projection is a clean
conversational prefix, not a forensic replay (that is ``replay.py``'s job).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable

from steerable_agent_protocol.generated import ToolCall

from .llm import LLMMessage

if TYPE_CHECKING:
    from .storage import StorageAdapter

#: Event kinds that carry transcript content.
_CONTENT = "content_delta"
_CALL_START = "tool_call_start"
_CALL_RESULT = "tool_call_result"
_CALL_ERROR = "tool_error"


def _payload(event: Any) -> dict[str, Any]:
    """Accept TraceEvent rows (``.payload``) or raw LoopEvent (``.data``)."""
    payload = getattr(event, "payload", None)
    if payload is None:
        payload = getattr(event, "data", None)
    return dict(payload or {})


def _kind(event: Any) -> str:
    return str(getattr(event, "kind", ""))


def project_transcript(events: Iterable[Any]) -> list[LLMMessage]:
    """Rebuild the loop's transcript from its recorded event stream.

    Events must be in emission order (``TraceEvent.sequence`` ascending).
    Returns messages ready to pass to ``CoreLoop.run`` — append the new user
    message and continue the session.
    """

    messages: list[LLMMessage] = []
    pending_text: list[str] = []
    pending_calls: list[ToolCall] = []

    def flush_assistant() -> None:
        nonlocal pending_text, pending_calls
        content = "".join(pending_text)
        calls = pending_calls
        pending_text = []
        pending_calls = []
        if not content and not calls:
            return
        messages.append(
            LLMMessage(
                role="assistant",
                content=content,
                tool_calls=calls or None,
            )
        )

    for event in events:
        kind = _kind(event)
        data = _payload(event)

        if kind == _CONTENT:
            delta = data.get("delta")
            if isinstance(delta, str) and delta:
                pending_text.append(delta)

        elif kind == _CALL_START:
            call_id = data.get("id")
            name = data.get("name")
            if not call_id or not name:
                continue
            pending_calls.append(
                ToolCall(
                    id=str(call_id),
                    name=str(name),
                    arguments=data.get("arguments") or {},
                )
            )

        elif kind in (_CALL_RESULT, _CALL_ERROR):
            # A result closes the assistant turn: text + calls become one
            # assistant message, then the tool message follows — mirroring
            # how the loop appends to its live transcript.
            if pending_text or pending_calls:
                flush_assistant()
            if kind == _CALL_ERROR:
                content = f"Error: {data.get('error', 'unknown error')}"
            else:
                result = data.get("result")
                content = result if isinstance(result, str) else str(
                    data.get("resultPreview") or ""
                )
                if not data.get("success", True) and data.get("error"):
                    content = content or f"Error: {data['error']}"
            messages.append(
                LLMMessage(
                    role="tool",
                    name=str(data.get("name") or ""),
                    tool_call_id=str(data.get("id") or ""),
                    content=content,
                )
            )

        # Everything else (stage_start/stage_complete/completion/usage/
        # soft_timeout/budget_exhausted/error/reasoning_delta…) is loop
        # bookkeeping, not transcript content.

    flush_assistant()
    return messages


async def load_transcript(storage: "StorageAdapter", trace_id: str) -> list[LLMMessage]:
    """Fetch a trace's events from storage and project them to a transcript."""

    events = await storage.list_events(trace_id)
    events.sort(key=lambda e: getattr(e, "sequence", 0))
    return project_transcript(events)
