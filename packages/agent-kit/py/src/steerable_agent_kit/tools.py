from __future__ import annotations

import contextvars
import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from steerable_agent_protocol import ToolCall, ToolResult

ToolMode = Literal["read", "safe_write", "destructive", "local"]


@dataclass
class ToolContext:
    """Runtime context injected into the tool handler."""
    user_id: str
    session_id: str
    db: Any                                # AsyncSession
    state: dict[str, Any]                  # From ChatLoop HookContext.state


# Callback can be sync or async, taking (ctx, call) and returning ToolResult or Any (coerced)
ToolHandler = (
    Callable[[ToolContext, ToolCall], ToolResult]
    | Callable[[ToolContext, ToolCall], Awaitable[ToolResult]]
    | Callable[[ToolContext, ToolCall], Any]
    | Callable[[ToolContext, ToolCall], Awaitable[Any]]
)


@dataclass(frozen=True)
class ToolSpec:
    name: str                              # e.g. "create_task"
    description: str
    json_schema: dict[str, Any]            # JSON Schema for arguments (fed to LLM)
    handler: ToolHandler
    mode: ToolMode = "read"                # for harness policy / decide_tool_mode
    tags: tuple[str, ...] = ()             # e.g. ("task",)


# ContextVar for thread-local/async-safe tool context access
current_tool_context: contextvars.ContextVar[ToolContext] = contextvars.ContextVar("current_tool_context")


def get_current_tool_context() -> ToolContext:
    """Helper to retrieve the current tool context from the ContextVar."""
    try:
        return current_tool_context.get()
    except LookupError:
        raise RuntimeError("No active ToolContext found in this context.")
