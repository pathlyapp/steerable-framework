from __future__ import annotations

from typing import Any


def is_terminal_result(result: dict[str, Any] | None) -> bool:
    if not result:
        return False
    if result.get("terminal") is True:
        return True
    if result.get("success") is False and result.get("needsFollowup") is not True:
        return True
    return False
