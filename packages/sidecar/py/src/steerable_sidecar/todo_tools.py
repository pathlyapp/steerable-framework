"""``todo_write`` registration — the session task list (CC TodoWrite parity).

Always on: the tool carries no workspace side effects and needs no host
wiring, so it is registered unconditionally at boot and advertised every
turn. One process-local store serves every chat the sidecar hosts, keyed by
chat id inside the tool.
"""

from __future__ import annotations

from steerable_agent_runtime import (
    TodoStore,
    ToolRouter,
    make_todo_write_tool,
)

#: Process lifetime — the sidecar hosts many chats; the store keys by chat.
_STORE = TodoStore()


def register_todo_write(router: ToolRouter) -> None:
    """Register ``todo_write`` on the sidecar's router (dispatch capability).

    Model visibility is separate: the per-turn assembly in ``sidecar.py``
    appends the descriptor to the tools array and intercepts dispatch
    locally, since the host does not know this tool.
    """
    tool = make_todo_write_tool(_STORE)
    meta = tool.__steerable_tool_meta__
    router.register(
        tool,
        name=meta["name"],
        mode=meta["mode"],
        description=meta["description"],
        schema=meta["schema"],
        require_consent=meta["require_consent"],
        concurrency_safe=meta["concurrency_safe"],
        exposure=meta["exposure"],
    )
