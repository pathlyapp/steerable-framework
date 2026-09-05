"""Event-driven system reminders (W2.2): a catalog of bounded, single-purpose
notice fragments plus the trigger table that binds them to observed failure
modes.

Why event-driven rather than periodic: instruction fade-out comes from
attention drifting toward the recent end of the context, so a reminder earns
its tokens only when the failure it addresses is actually observable — and
it must land at the highest-recency position (appended right before the next
LLM request via ``pre_step``), not as a re-sent system prompt.

The catalog abstracts the loop's three existing notice fragments
(``SoftTimeoutNotice`` / ``DisciplineRetryNotice`` / ``NarrationRequest``)
into named entries alongside the new failure-mode reminders. Every entry is
a ``ContextFragment``: bounded by the fragment token gate, self-recognisable
via markers, and injected through the ``append_fragment`` enforcement point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .history import ContextFragment
from .hooks import NoopHooks, PreStepAction, TranscriptAppend
from .llm import LLMMessage

# ---------------------------------------------------------------------------
# Reminder fragments — one per failure mode, each single-purpose and bounded
# ---------------------------------------------------------------------------


class ErrorStreakReminder(ContextFragment):
    """Consecutive tool errors approaching the circuit breaker."""

    content_kind = "reminder.error_streak"
    max_tokens = 200

    def __init__(self, streak: int, limit: int) -> None:
        self._streak = streak
        self._limit = limit

    def body(self) -> str:
        return (
            f"[system notice] Tool-call error streak: {self._streak} in a row "
            f"(the run stops at {self._limit}). Stop retrying the same shape: "
            "read the error, change the approach, or state the blocker."
        )

    @classmethod
    def type_markers(cls) -> tuple[str, str]:
        return ("[system notice] Tool-call error streak:", "")


class AbandonedRecoveryReminder(ContextFragment):
    """The turn is ending while tool errors stand unrecovered."""

    content_kind = "reminder.abandoned_recovery"
    max_tokens = 200

    def body(self) -> str:
        return (
            "[system notice] The last tool call(s) failed and the turn is "
            "about to end. Either recover (fix the call and retry once) or "
            "tell the user explicitly what failed and why — do not end "
            "silently on an error."
        )

    @classmethod
    def type_markers(cls) -> tuple[str, str]:
        return ("[system notice] The last tool call(s) failed", "")


class RunawayExplorationReminder(ContextFragment):
    """Many tool calls without a write since the last one."""

    content_kind = "reminder.runaway_exploration"
    max_tokens = 200

    def __init__(self, calls: int) -> None:
        self._calls = calls

    def body(self) -> str:
        return (
            f"[system notice] {self._calls} tool calls since the last write. "
            "If the required files already exist, run them; if they do "
            "not, write them now. Do not keep inspecting source."
        )

    @classmethod
    def type_markers(cls) -> tuple[str, str]:
        return ("[system notice]", "Do not keep inspecting source.")


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReminderEntry:
    """One catalog row: the fragment and the observed failure it binds to.

    ``failure_mode`` is the audit trail required by W2.2.2 — every rule must
    name the real failure it addresses, not a round cadence.
    """

    id: str
    failure_mode: str
    fragment: type[ContextFragment]


def _soft_timeout_fragment() -> type[ContextFragment]:
    from .loop import SoftTimeoutNotice

    return SoftTimeoutNotice


def _discipline_retry_fragment() -> type[ContextFragment]:
    from .loop import DisciplineRetryNotice

    return DisciplineRetryNotice


def _narration_fragment() -> type[ContextFragment]:
    from .loop import NarrationRequest

    return NarrationRequest


#: The catalog. Ids are stable — traces and tests reference them.
REMINDER_CATALOG: tuple[ReminderEntry, ...] = (
    ReminderEntry(
        id="loop.soft_timeout",
        failure_mode="时间预算将尽而未收尾（软超时后仍开始新工作）",
        fragment=_soft_timeout_fragment(),
    ),
    ReminderEntry(
        id="loop.discipline_retry",
        failure_mode="提前宣布完成：声称执行了动作但未发出任何工具调用",
        fragment=_discipline_retry_fragment(),
    ),
    ReminderEntry(
        id="loop.narration",
        failure_mode="任务结束无自然语言总结（纯工具轨迹收尾）",
        fragment=_narration_fragment(),
    ),
    ReminderEntry(
        id="recovery.error_streak",
        failure_mode="连续工具错误逼近熔断阈值仍同形重试",
        fragment=ErrorStreakReminder,
    ),
    ReminderEntry(
        id="recovery.abandoned",
        failure_mode="工具错误未恢复即结束回合，对用户隐瞒失败",
        fragment=AbandonedRecoveryReminder,
    ),
    ReminderEntry(
        id="exploration.runaway",
        failure_mode="探索失控：连续非写入调用，含写过之后继续只读",
        fragment=RunawayExplorationReminder,
    ),
)


def reminder_entry(reminder_id: str) -> ReminderEntry:
    """Catalog lookup; unknown ids fail loud (a typo must not silently
    disable a reminder)."""
    for entry in REMINDER_CATALOG:
        if entry.id == reminder_id:
            return entry
    known = [e.id for e in REMINDER_CATALOG]
    raise KeyError(f"unknown reminder {reminder_id!r}; catalog: {known}")


# ---------------------------------------------------------------------------
# Trigger rules (W2.2.2) + the hooks that fire them (W2.2.3)
# ---------------------------------------------------------------------------

#: Tool names that count as producing an artifact (writes/edits), for the
#: runaway-exploration rule. Hosts with different tool names pass their own.
DEFAULT_WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})


@dataclass(frozen=True, slots=True)
class ReminderRules:
    """Trigger thresholds. Every rule names its reminder and therefore its
    observed failure mode; there is no round-cadence rule by design."""

    #: Fire ``recovery.error_streak`` when consecutive errors reach this
    #: fraction of the loop's circuit breaker.
    error_streak_ratio: float = 0.5
    #: Fire ``exploration.runaway`` after this many tool calls without a
    #: write since the last one (or since the start).
    runaway_calls: int = 12
    #: Re-fire a still-true rule after this many rounds (fade-out is the
    #: point, but every-round spam is noise).
    refire_rounds: int = 6


class ReminderHooks(NoopHooks):
    """Fires catalog reminders from observed loop state.

    ``post_tool_result`` tracks error streaks and consecutive non-write
    calls (a write resets that streak; a prior write does not silence
    later inspect-only runs). ``pre_step`` appends a due reminder as the
    last transcript item before the request. Each rule fires once, then
    re-fires only after ``refire_rounds`` while its condition still holds.
    """

    def __init__(
        self,
        *,
        max_tool_errors: int,
        rules: ReminderRules | None = None,
        write_tools: frozenset[str] = DEFAULT_WRITE_TOOLS,
    ) -> None:
        self._rules = rules or ReminderRules()
        self._max_tool_errors = max_tool_errors
        self._write_tools = write_tools
        self._since_write = 0
        self._fired_at: dict[str, int] = {}

    async def post_tool_result(self, result: Any, call: Any, ctx: Any) -> Any:
        if call.name in self._write_tools and getattr(result, "success", False):
            self._since_write = 0
        else:
            self._since_write += 1
        return result

    async def pre_step(self, transcript: Any, ctx: Any) -> PreStepAction:
        due = self._due(ctx)
        if due is None:
            return PreStepAction(kind="proceed")
        entry = reminder_entry(due)
        fragment = self._render(entry, ctx)
        return PreStepAction(
            kind="proceed",
            appends=[
                TranscriptAppend(
                    message=LLMMessage.text_of(fragment.role, fragment.render()),
                    kind=entry.fragment.content_kind,
                    fragment=fragment,
                )
            ],
            append_action="reminder",
        )

    def _due(self, ctx: Any) -> str | None:
        round_index = getattr(ctx, "round_index", 0)

        def ready(reminder_id: str) -> bool:
            last = self._fired_at.get(reminder_id)
            return last is None or round_index - last >= self._rules.refire_rounds

        streak = getattr(ctx, "consecutive_tool_errors", 0)
        if (
            streak >= max(1, int(self._max_tool_errors * self._rules.error_streak_ratio))
            and streak < self._max_tool_errors
            and ready("recovery.error_streak")
        ):
            self._fired_at["recovery.error_streak"] = round_index
            return "recovery.error_streak"
        if (
            self._since_write >= self._rules.runaway_calls
            and ready("exploration.runaway")
        ):
            self._fired_at["exploration.runaway"] = round_index
            return "exploration.runaway"
        return None

    def _render(self, entry: ReminderEntry, ctx: Any) -> ContextFragment:
        if entry.id == "recovery.error_streak":
            return ErrorStreakReminder(
                getattr(ctx, "consecutive_tool_errors", 0), self._max_tool_errors
            )
        if entry.id == "exploration.runaway":
            return RunawayExplorationReminder(self._since_write)
        return entry.fragment()
