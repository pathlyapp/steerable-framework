"""W2.2: reminder catalog, trigger rules, highest-recency injection."""

from __future__ import annotations

import pytest
from steerable_agent_protocol.generated import ToolCall, ToolResult

from steerable_agent_runtime.history import ContextFragment
from steerable_agent_runtime.reminders import (
    REMINDER_CATALOG,
    AbandonedRecoveryReminder,
    ErrorStreakReminder,
    ReminderHooks,
    ReminderRules,
    RunawayExplorationReminder,
    reminder_entry,
)


class _Ctx:
    def __init__(self, round_index: int = 0, consecutive_tool_errors: int = 0) -> None:
        self.round_index = round_index
        self.consecutive_tool_errors = consecutive_tool_errors
        self.chat_id = None


# -- catalog (2.2.1 / 2.2.2) ---------------------------------------------------


def test_catalog_entries_are_single_purpose_bounded_fragments() -> None:
    assert len(REMINDER_CATALOG) >= 6
    ids = [e.id for e in REMINDER_CATALOG]
    assert len(ids) == len(set(ids)), "catalog ids must be unique"
    for entry in REMINDER_CATALOG:
        # 2.2.2: every entry names the observed failure it binds to.
        assert entry.failure_mode.strip(), entry.id
        # 2.2.1: every entry is a bounded ContextFragment (the CI gate walks
        # subclasses; here the catalog asserts membership directly).
        assert issubclass(entry.fragment, ContextFragment), entry.id
        assert entry.fragment.effective_max_tokens() is not None


def test_catalog_covers_the_three_legacy_notices() -> None:
    kinds = {entry.fragment.content_kind for entry in REMINDER_CATALOG}
    assert "loop.soft_timeout_notice" in kinds
    assert "loop.discipline_retry_notice" in kinds
    assert "loop.narration_request" in kinds


def test_unknown_reminder_id_fails_loud() -> None:
    with pytest.raises(KeyError, match="unknown reminder"):
        reminder_entry("no.such.reminder")


# -- triggers (2.2.2) ----------------------------------------------------------


@pytest.mark.asyncio
async def test_error_streak_fires_near_the_breaker() -> None:
    hooks = ReminderHooks(max_tool_errors=4)
    action = await hooks.pre_step([], _Ctx(round_index=1, consecutive_tool_errors=2))
    assert action.appends, "streak at 50% of the breaker should fire"
    fragment = action.appends[0].fragment
    assert isinstance(fragment, ErrorStreakReminder)
    assert "Tool-call error streak: 2 in a row" in fragment.render()


@pytest.mark.asyncio
async def test_error_streak_stays_silent_below_threshold_and_at_breaker() -> None:
    hooks = ReminderHooks(max_tool_errors=4)
    below = await hooks.pre_step([], _Ctx(round_index=1, consecutive_tool_errors=1))
    assert not below.appends
    # At the breaker the loop stops anyway — a reminder would be noise.
    at = await hooks.pre_step([], _Ctx(round_index=1, consecutive_tool_errors=4))
    assert not at.appends


@pytest.mark.asyncio
async def test_runaway_exploration_fires_without_writes() -> None:
    hooks = ReminderHooks(max_tool_errors=4, rules=ReminderRules(runaway_calls=3))
    ctx = _Ctx(round_index=1)
    for i in range(3):
        await hooks.post_tool_result(
            ToolResult(success=True, data={"v": i}),
            ToolCall(id=str(i), name="read_file", arguments={}),
            ctx,
        )
    action = await hooks.pre_step([], ctx)
    assert action.appends
    assert isinstance(action.appends[0].fragment, RunawayExplorationReminder)


@pytest.mark.asyncio
async def test_runaway_exploration_silent_right_after_a_write() -> None:
    hooks = ReminderHooks(max_tool_errors=4, rules=ReminderRules(runaway_calls=3))
    ctx = _Ctx(round_index=1)
    for i in range(3):
        await hooks.post_tool_result(
            ToolResult(success=True, data={}),
            ToolCall(id=str(i), name="read_file", arguments={}),
            ctx,
        )
    await hooks.post_tool_result(
        ToolResult(success=True, data={}),
        ToolCall(id="w", name="write_file", arguments={}),
        ctx,
    )
    action = await hooks.pre_step([], ctx)
    assert not action.appends


@pytest.mark.asyncio
async def test_runaway_exploration_fires_after_write_then_inspect() -> None:
    """make-mips 33547943349: vm.js existed, then 200 bash greps. A lifetime
    write flag would have silenced the reminder for the rest of the run."""
    hooks = ReminderHooks(max_tool_errors=4, rules=ReminderRules(runaway_calls=3))
    ctx = _Ctx(round_index=1)
    await hooks.post_tool_result(
        ToolResult(success=True, data={}),
        ToolCall(id="w", name="write_file", arguments={}),
        ctx,
    )
    for i in range(3):
        await hooks.post_tool_result(
            ToolResult(success=True, data={}),
            ToolCall(id=str(i), name="read_file", arguments={}),
            ctx,
        )
    action = await hooks.pre_step([], ctx)
    assert action.appends
    fragment = action.appends[0].fragment
    assert isinstance(fragment, RunawayExplorationReminder)
    assert "3 tool calls since the last write" in fragment.render()


@pytest.mark.asyncio
async def test_refire_waits_for_the_configured_gap() -> None:
    hooks = ReminderHooks(
        max_tool_errors=4, rules=ReminderRules(refire_rounds=5)
    )
    first = await hooks.pre_step([], _Ctx(round_index=1, consecutive_tool_errors=2))
    assert first.appends
    immediate = await hooks.pre_step([], _Ctx(round_index=2, consecutive_tool_errors=2))
    assert not immediate.appends
    later = await hooks.pre_step([], _Ctx(round_index=6, consecutive_tool_errors=2))
    assert later.appends


# -- fragments -----------------------------------------------------------------


def test_new_fragments_render_bounded_marked_text() -> None:
    for fragment in (
        ErrorStreakReminder(2, 4),
        AbandonedRecoveryReminder(),
        RunawayExplorationReminder(15),
    ):
        text = fragment.render()
        assert text.startswith("[system notice]")
        assert fragment.matches_text(text)
