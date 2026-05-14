from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ToolMode = Literal["read", "safe_write", "destructive", "other"]


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    tool_mode: ToolMode
    reason: str


def decide_tool_mode(tool_name: str) -> ToolMode:
    normalized = tool_name.lower()
    if normalized.startswith(("get_", "list_", "read_")):
        return "read"
    if normalized.startswith(("create_", "update_", "set_", "write_", "apply_")):
        return "safe_write"
    if normalized.startswith(("delete_", "drop_", "remove_", "destroy_")):
        return "destructive"
    return "other"
