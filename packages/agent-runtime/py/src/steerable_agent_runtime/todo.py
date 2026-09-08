"""Session task list — the ``todo_write`` tool.

The model maintains one ordered task list per chat (Claude Code
``TodoWrite`` parity): multi-step work stays legible across rounds because
the model rewrites the list as it progresses, and every rewrite lands in the
durable message record as the tool result, so a resumed session rebuilds the
list from the transcript.

Semantics are full-replace: each call carries the complete list, not a diff.
The store keys lists by ``chat_id`` from the dispatch context, so concurrent
chats in one sidecar process never see each other's tasks.
"""

from __future__ import annotations

from typing import Any

from .errors import ToolDispatchError

#: The tool name the model calls.
TODO_TOOL_NAME = "todo_write"

#: Valid task states, in lifecycle order.
TODO_STATUSES = ("pending", "in_progress", "completed")

#: Bounds on the list itself. The list is a working set, not a backlog —
#: past ~50 items the model is planning, not tracking, and the rendered
#: result starts to lean on the fragment budget.
_MIN_TODOS = 1
_MAX_TODOS = 50

#: Model-facing description, shared by the registration metadata and the
#: OpenAI descriptor.
_DESCRIPTION = (
    "Track multi-step work as an ordered task list. Create the list when a "
    "task needs several steps; rewrite it as you progress — mark the task "
    "you are working on in_progress (at most one) and completed the moment "
    "it is done. Each call replaces the whole list. Skip it for single-step "
    "requests."
)

#: JSON Schema for the tool's arguments — one full-replace list.
TODO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "minItems": _MIN_TODOS,
            "maxItems": _MAX_TODOS,
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Stable identifier for this task.",
                    },
                    "content": {
                        "type": "string",
                        "description": "One line describing the task.",
                    },
                    "status": {
                        "type": "string",
                        "enum": list(TODO_STATUSES),
                    },
                },
                "required": ["id", "content", "status"],
            },
            "description": (
                "The complete task list, replacing the previous one. "
                "Mark exactly the task you are working on as in_progress; "
                "keep at most one in_progress at a time."
            ),
        },
    },
    "required": ["todos"],
}


class TodoStore:
    """Process-local task lists keyed by chat id.

    Persistence comes for free from the message record — every rewrite is a
    tool result in the transcript, and full-replace semantics make the state
    self-healing: after a restart the model's next call rewrites the whole
    list from what it sees in the resumed history.
    """

    def __init__(self) -> None:
        self._by_chat: dict[str, list[dict[str, Any]]] = {}

    def get(self, chat_id: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self._by_chat.get(chat_id, [])]

    def set(self, chat_id: str, todos: list[dict[str, Any]]) -> None:
        self._by_chat[chat_id] = [dict(item) for item in todos]


def _normalize_todos(todos: Any) -> list[dict[str, Any]]:
    """Validate one full-replace list and return it in canonical shape.

    A violation raises ``ToolDispatchError`` — the router wraps it as the
    tool result, so the model sees exactly what to fix and retries within
    the same turn.
    """
    if not isinstance(todos, list):
        raise ToolDispatchError(
            f"todo_write: todos must be an array, got {type(todos).__name__}"
        )
    if not (_MIN_TODOS <= len(todos) <= _MAX_TODOS):
        raise ToolDispatchError(
            f"todo_write: todos must contain {_MIN_TODOS}-{_MAX_TODOS} items, "
            f"got {len(todos)}. Track the current working set, not a backlog."
        )
    seen_ids: set[str] = set()
    in_progress = 0
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(todos):
        if not isinstance(item, dict):
            raise ToolDispatchError(
                f"todo_write: todos[{index}] must be an object, "
                f"got {type(item).__name__}"
            )
        task_id = item.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ToolDispatchError(
                f"todo_write: todos[{index}] is missing a non-empty string "
                '"id".'
            )
        if task_id in seen_ids:
            raise ToolDispatchError(
                f'todo_write: duplicate id "{task_id}" — ids must be unique.'
            )
        seen_ids.add(task_id)
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ToolDispatchError(
                f'todo_write: todos[{index}] ("{task_id}") is missing a '
                'non-empty string "content".'
            )
        status = item.get("status")
        if status not in TODO_STATUSES:
            raise ToolDispatchError(
                f'todo_write: todos[{index}] ("{task_id}") has invalid status '
                f"{status!r}; expected one of {', '.join(TODO_STATUSES)}."
            )
        if status == "in_progress":
            in_progress += 1
        normalized.append({"id": task_id, "content": content, "status": status})
    if in_progress > 1:
        raise ToolDispatchError(
            f"todo_write: {in_progress} tasks are in_progress; keep at most "
            "one — mark the rest pending or completed."
        )
    return normalized


def make_todo_write_tool(store: TodoStore) -> Any:
    """Build the ``todo_write`` tool handler bound to ``store``.

    The returned coroutine function carries the registration metadata the
    router reads (name, description, schema) so a host registers it with one
    call. Marked ``concurrency_safe=False``: two rewrites of the same chat's
    list would race, and last-writer-wins is the wrong resolution for a
    full-replace update.
    """

    async def todo_write(
        todos: list[dict[str, Any]], context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        normalized = _normalize_todos(todos)
        chat_id = str((context or {}).get("chat_id") or "")
        store.set(chat_id, normalized)
        counts = {status: 0 for status in TODO_STATUSES}
        for item in normalized:
            counts[item["status"]] += 1
        return {
            "todos": normalized,
            "summary": {
                "total": len(normalized),
                "pending": counts["pending"],
                "inProgress": counts["in_progress"],
                "completed": counts["completed"],
            },
        }

    todo_write.__steerable_tool_meta__ = {  # type: ignore[attr-defined]
        "name": TODO_TOOL_NAME,
        "mode": "read",  # session state only; no workspace side effects
        "description": _DESCRIPTION,
        "schema": TODO_SCHEMA,
        "require_consent": False,
        "concurrency_safe": False,
        "exposure": "direct",
    }
    return todo_write


def todo_write_tool_descriptor() -> dict[str, Any]:
    """OpenAI tool schema to append to the model's tools list.

    The sidecar registers ``todo_write`` on its router (so dispatch works on
    both the host path and the sidecar-local path) but the model only sees it
    when the descriptor is appended to the ``tools`` array — mirroring how
    run_code advertises itself.
    """
    return {
        "type": "function",
        "function": {
            "name": TODO_TOOL_NAME,
            "description": _DESCRIPTION,
            "parameters": TODO_SCHEMA,
        },
    }
