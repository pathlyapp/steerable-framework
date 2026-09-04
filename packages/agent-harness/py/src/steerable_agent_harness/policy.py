from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ToolMode = Literal["read", "safe_write", "destructive", "other"]


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    tool_mode: ToolMode
    reason: str


#: Side-effect-free network reads the prefix rules cannot reach. Exact
#: names, not a ``web_`` prefix: a future ``web_deploy``-style tool must not
#: inherit the read posture. The approval algebra reads this table
#: (``ApprovalRequest.mode``), so the classification decides both the
#: prompt's risk label and the headless AutoApprover's default verdict.
_READ_EXACT = frozenset({"web_search", "web_fetch"})


def decide_tool_mode(tool_name: str) -> ToolMode:
    normalized = tool_name.lower()
    if normalized in _READ_EXACT:
        return "read"
    if normalized.startswith(("get_", "list_", "read_")):
        return "read"
    if normalized.startswith(("create_", "update_", "set_", "write_", "apply_")):
        return "safe_write"
    if normalized.startswith(("delete_", "drop_", "remove_", "destroy_")):
        return "destructive"
    return "other"
