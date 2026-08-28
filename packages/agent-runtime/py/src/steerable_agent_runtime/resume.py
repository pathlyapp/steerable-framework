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

Crash recovery: a trace that ends mid-tool-execution leaves dangling
tool_calls on the last assistant message. The projection closes them with a
synthetic "result unknown — interrupted" tool message so providers accept
the resumed transcript (mirrors dsh's cold-start ``TOOL_OUTCOME_UNKNOWN``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable

from steerable_agent_protocol.generated import ToolCall

from .llm import LLMMessage

if TYPE_CHECKING:
    from .history import HistoryStore
    from .storage import StorageAdapter

#: Event kinds that carry transcript content.
_CONTENT = "content_delta"
_CALL_START = "tool_call_start"
_CALL_RESULT = "tool_call_result"
_CALL_ERROR = "tool_error"

#: Synthetic tool message closing a tool_call whose result was never
#: recorded — the process died (or the stream was killed) between
#: ``tool_call_start`` and the outcome event. dsh does the same on cold
#: start with ``TOOL_OUTCOME_UNKNOWN``; without it, providers reject the
#: resumed transcript for dangling tool_calls.
_INTERRUPTED_RESULT = (
    "[result unknown — the process was interrupted before this tool call "
    "completed. Do not claim it succeeded; verify any side effects before "
    "relying on them.]"
)


def _payload(event: Any) -> dict[str, Any]:
    """Accept TraceEvent rows (``.payload``) or raw LoopEvent (``.data``)."""
    payload = getattr(event, "payload", None)
    if payload is None:
        payload = getattr(event, "data", None)
    return dict(payload or {})


def _kind(event: Any) -> str:
    return str(getattr(event, "kind", ""))


def project_transcript(
    events: Iterable[Any],
    *,
    until_sequence: int | None = None,
) -> list[LLMMessage]:
    """Rebuild the loop's transcript from its recorded event stream.

    Events must be in emission order (``TraceEvent.sequence`` ascending).
    Returns messages ready to pass to ``CoreLoop.run`` — append the new user
    message and continue the session.

    ``until_sequence`` (inclusive) truncates the event stream before
    projecting — the fork primitive for variant/regenerate flows: the
    caller locates the fork point via ``trace.fetch`` (e.g. the sequence of
    the ``tool_call_start`` to exclude onward, or the completion to stop
    after) and re-runs from the truncated transcript. Truncation needs
    stored events (``TraceEvent.sequence``); raw LoopEvents without a
    sequence attribute are treated as sequence 0.
    """

    if until_sequence is not None:
        events = [
            e for e in events if getattr(e, "sequence", 0) <= until_sequence
        ]

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
            LLMMessage.text_of(
                "assistant",
                content,
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
                LLMMessage.text_of(
                    "tool",
                    content,
                    name=str(data.get("name") or ""),
                    tool_call_id=str(data.get("id") or ""),
                )
            )

        # Everything else (stage_start/stage_complete/completion/usage/
        # soft_timeout/budget_exhausted/error/reasoning_delta…) is loop
        # bookkeeping, not transcript content.

    flush_assistant()
    return _close_dangling_tool_calls(messages)


def _close_dangling_tool_calls(messages: list[LLMMessage]) -> list[LLMMessage]:
    """Append synthetic tool messages for tool_calls with no recorded result.

    A trace that ends mid-tool-execution (crash, kill, lost stream) projects
    to an assistant message whose tool_calls were never answered; most
    providers reject such a transcript outright. Closing them with an
    explicit "interrupted" marker keeps the session resumable and tells the
    model not to trust the unknown outcome. Complete traces pass through
    unchanged.
    """
    out: list[LLMMessage] = []
    i = 0
    while i < len(messages):
        message = messages[i]
        out.append(message)
        i += 1
        if message.role != "assistant" or not message.tool_calls:
            continue
        # Copy the following tool-message block first, then append synthetic
        # closures for the unanswered calls — synthetic messages go AFTER the
        # recorded ones so the original ordering is preserved.
        answered: set[str] = set()
        while i < len(messages) and messages[i].role == "tool":
            tool_msg = messages[i]
            if tool_msg.tool_call_id:
                answered.add(tool_msg.tool_call_id)
            out.append(tool_msg)
            i += 1
        for call in message.tool_calls:
            if call.id not in answered:
                out.append(
                    LLMMessage.text_of(
                        "tool",
                        _INTERRUPTED_RESULT,
                        name=call.name,
                        tool_call_id=call.id,
                    )
                )
    return out


async def load_transcript(
    storage: "StorageAdapter",
    trace_id: str,
    *,
    until_sequence: int | None = None,
) -> list[LLMMessage]:
    """Fetch a trace's events from storage and project them to a transcript.

    ``until_sequence`` forks the projection — see ``project_transcript``.
    """

    events = await storage.list_events(trace_id)
    events.sort(key=lambda e: getattr(e, "sequence", 0))
    return project_transcript(events, until_sequence=until_sequence)


#: Page size for the reverse boundary scan. A run that compacted recently
#: finds its boundary in the first page; a never-compacted long record pages
#: back to the start — bounded work, and the common case stays O(tail).
_RESUME_PAGE = 256


async def load_history_transcript(
    storage: "HistoryStore",
    record_id: str,
    *,
    until_seq: int | None = None,
) -> list[LLMMessage] | None:
    """Resume from the durable record (Wave 1) — O(tail), boundary-aware.

    Scans backwards (newest-first pages) for the newest
    ``compaction.boundary`` entry, then projects only the entries after it —
    the superseded span is never read. Returns ``None`` when the record has
    no entries (caller falls back to the trace-event projection or starts
    fresh).

    ``until_seq`` (inclusive) truncates the record before projecting — the
    record-level fork primitive: seed a new record with the returned prefix
    (``ContextManager.seed``) and continue there.
    """

    from .history import (  # local: keeps the trace path import-light
        CompactionBoundary,
        HistoryItem,
        HistorySeed,
        entry_from_dict,
    )

    boundary_seq = -1
    cursor = until_seq
    while True:
        page = await storage.list_history(
            record_id,
            until_seq=cursor,
            limit=_RESUME_PAGE,
            reverse=True,
        )
        if not page:
            return None
        for raw in page:
            if raw.get("entry") == "boundary":
                boundary_seq = int(raw["seq"])
                break
        if boundary_seq >= 0 or len(page) < _RESUME_PAGE:
            break
        cursor = int(page[-1]["seq"]) - 1

    entries = await storage.list_history(
        record_id, after_seq=boundary_seq, until_seq=until_seq
    )
    messages: list[LLMMessage] = []
    for raw in entries:
        entry = entry_from_dict(raw)
        if isinstance(entry, HistoryItem):
            messages.append(entry.message)
        elif isinstance(entry, HistorySeed):
            messages.extend(entry.messages)
        elif isinstance(entry, CompactionBoundary):
            # A boundary inside the forward range means the reverse scan's
            # boundary wasn't the newest (concurrent writer) — restart the
            # projection from here; the tail after it is the visible span.
            messages = []
    return messages
