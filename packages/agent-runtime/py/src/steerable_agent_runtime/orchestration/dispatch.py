"""Generic orchestration dispatch and decision-making logic."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GroupChatStatus:
    """Snapshot of a chat's multi-agent participation history."""

    is_group: bool
    member_agent_ids: list[str] = field(default_factory=list)
    last_speaker_agent_id: str | None = None


@dataclass(frozen=True)
class OrchestrationDecision:
    """Decision produced by the orchestration dispatcher."""

    should_orchestrate: bool
    mode: str  # "single" | "explicit" | "groupchat"
    allowed_agent_ids: list[str] = field(default_factory=list)
    fallback_agent_id: str | None = None


def decide_orchestration(
    *,
    explicit_mentions: Sequence[str],
    group_status: GroupChatStatus,
) -> OrchestrationDecision:
    """Run the 4-state orchestration decision tree.

    +------------------------+--------------+--------------------+
    | explicit @ count       | groupchat?   | decision           |
    +========================+==============+====================+
    | >= 2                   | any          | orchestrate (explicit) |
    +------------------------+--------------+--------------------+
    | == 1                   | any          | single (escape hatch)  |
    +------------------------+--------------+--------------------+
    | == 0                   | True         | orchestrate (groupchat) |
    +------------------------+--------------+--------------------+
    | == 0                   | False        | single                  |
    +------------------------+--------------+--------------------+
    """
    distinct_mentions = list(dict.fromkeys(filter(None, explicit_mentions)))
    explicit_count = len(distinct_mentions)

    if explicit_count >= 2:
        return OrchestrationDecision(
            should_orchestrate=True,
            mode="explicit",
            allowed_agent_ids=distinct_mentions,
            fallback_agent_id=group_status.last_speaker_agent_id,
        )
    if explicit_count == 1:
        return OrchestrationDecision(
            should_orchestrate=False,
            mode="single",
            allowed_agent_ids=[],
            fallback_agent_id=None,
        )
    if group_status.is_group:
        return OrchestrationDecision(
            should_orchestrate=True,
            mode="groupchat",
            allowed_agent_ids=list(group_status.member_agent_ids),
            fallback_agent_id=group_status.last_speaker_agent_id,
        )
    return OrchestrationDecision(
        should_orchestrate=False,
        mode="single",
        allowed_agent_ids=[],
        fallback_agent_id=None,
    )
