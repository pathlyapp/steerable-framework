"""Translate standard framework SSEEvents into orchestration-flavoured SSEEvents.

This routes streaming assistant deltas from worker-specific runs into the correct
subtask card on the UI.
"""

from __future__ import annotations

from typing import Optional

from steerable_agent_protocol.generated import SSEEvent
from steerable_agent_runtime.transport import RuntimeSSEType

_DROPPED_AGENT_EVENTS = frozenset(
    {
        "session.start",
        "session.end",
        "round_start",
        "round_end",
        "compression_start",
        "compression_complete",
    },
)


def reframe_worker_event(
    event: SSEEvent,
    *,
    group_id: str,
    task_id: str,
) -> Optional[SSEEvent]:
    """Translate one framework SSEEvent into an orchestration SSEEvent.

    Returns None when the event is purely internal or handled at the executor level.
    """
    event_type = event.type

    # Content delta -> wrapped as task_chunk
    if event_type == RuntimeSSEType.CONTENT.value:
        payload = event.payload or {}
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text:
            return None
        return SSEEvent(
            type="orchestration",
            event="task_chunk",
            orchestrationGroupId=group_id,
            taskId=task_id,
            content=text,
        )

    # Lifecycles -> drop or pass-through
    if event_type == RuntimeSSEType.AGENT.value:
        sub_event = event.event or ""
        if sub_event in _DROPPED_AGENT_EVENTS:
            return None
        return _stamp_event(event, group_id=group_id, task_id=task_id)

    if event_type == RuntimeSSEType.DONE.value:
        return None
    if event_type == RuntimeSSEType.MESSAGE_ID.value:
        return None

    # tool_call / error / etc -> preserve with stamped taskId
    return _stamp_event(event, group_id=group_id, task_id=task_id)


def _stamp_event(
    event: SSEEvent,
    *,
    group_id: str,
    task_id: str,
) -> SSEEvent:
    """Return a new SSEEvent with orchestration identity fields set."""
    copy = event.model_copy(deep=True)
    copy.orchestrationGroupId = group_id
    copy.taskId = task_id
    return copy
