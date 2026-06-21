from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class EntitlementDecision:
    allowed: bool
    reason: str = ""
    upgrade_hint: str | None = None        # e.g. "升级 Pro 解锁该功能"


@runtime_checkable
class EntitlementGate(Protocol):
    """Protocol for checking user entitlements, membership tiers, or quotas."""

    async def check(
        self,
        *,
        user_id: str,
        key: str,
        amount: int = 1,
    ) -> EntitlementDecision:
        """Check if user has entitlement for key (e.g. 'tool:create_task' or 'model:gpt-4')."""
        ...
