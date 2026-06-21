"""Completion-decision utilities.

Two layers:

* ``is_terminal_result(result_dict)`` — the original predicate, kept for back
  compat (existing call sites in the host API still use it).
* ``decide_completion(...)`` — the higher-level dispatcher used by the
  framework ``ChatLoop``. Takes the round's tool calls + tool results +
  budget state + budget limits and returns a single ``CompletionDecision``
  the loop can act on.

Both are pure, deterministic, and side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .budget import BudgetLimit, BudgetState


CompletionStatus = Literal["executing", "completed", "failed", "budget_exhausted"]
"""The four terminal-or-continue states the loop scheduler reasons about."""


LimitKind = Literal["tokens", "steps", "tool_calls", "rounds", "time"]
"""Which budget dimension triggered ``budget_exhausted``.

The three *native* dimensions checked by ``decide_completion`` against the
harness ``BudgetLimit``:

* ``tokens``     — cumulative token usage
* ``steps``      — number of completed scheduler steps
* ``tool_calls`` — number of dispatched tool calls

Two additional dimensions are reported only by *callers* that own caps the
harness budget cannot see:

* ``rounds``     — ChatLoop's ``max_rounds`` safety net (framework-loop only)
* ``time``       — ChatLoop's ``max_elapsed_seconds`` wall-clock guard
                   (framework-loop only)

The harness predicate itself never returns ``rounds`` or ``time``; those land
in synthetic ``CompletionDecision`` objects that the loop constructs.
"""


@dataclass(slots=True)
class CompletionDecision:
    """The output of one ``decide_completion`` call.

    ``status`` is the only field callers strictly need; everything else
    explains *why* the status was chosen and is meant for tracing / UX.
    """

    status: CompletionStatus
    reason: str
    limit_kind: LimitKind | None = None
    terminal_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Stable dict form used when the loop populates
        ``RoundEndCtx.decision`` / ``LoopEndCtx.final_decision``.
        """
        return {
            "status": self.status,
            "reason": self.reason,
            "limit_kind": self.limit_kind,
            "terminal_index": self.terminal_index,
        }


def is_terminal_result(result: dict[str, Any] | None) -> bool:
    """True if a tool result, on its own, terminates the loop.

    A result is terminal when:
    * the handler explicitly set ``terminal: True``, or
    * the handler reported failure (``success: False``) AND did not request a
      follow-up (``needsFollowup`` is not True). Errors that *do* request a
      follow-up are considered self-healing and don't terminate.

    Kept for back compat. New code should prefer ``decide_completion``, which
    composes this predicate with the budget and the assistant's tool-call
    state into a single decision.
    """
    if not result:
        return False
    if result.get("terminal") is True:
        return True
    if result.get("success") is False and result.get("needsFollowup") is not True:
        return True
    return False


def decide_completion(
    *,
    tool_calls: list[Any] | None,
    tool_results: list[dict[str, Any]] | None,
    budget_state: BudgetState,
    budget_limits: BudgetLimit | None = None,
    finish_reason: str | None = None,
) -> CompletionDecision:
    """Decide whether the loop should continue, stop, fail, or report
    budget exhaustion. Pure and deterministic — no I/O, no clock reads.

    **Decision tree, in priority order:**

    1. ``budget_limits`` set and any dimension exceeded → ``budget_exhausted``
       with the corresponding ``limit_kind``. Checked first so a runaway
       round still terminates even if the model just returned a terminal
       result.
    2. The assistant produced no tool calls (``tool_calls`` is empty/None) →
       ``completed`` with ``reason="no_tool_calls"``. This is the natural
       stop: the model decided it had nothing more to do.
    3. Any tool result with ``terminal=True`` →
        a. If ``success=True`` → ``completed`` with the result's index.
        b. If ``success=False`` → ``failed`` with the result's index.
    4. Every tool result is a non-followup failure (``is_terminal_result``
       returns True for all of them) → ``failed``.
    5. Otherwise → ``executing`` (the loop should run another round).

    ``finish_reason`` is accepted for future use (e.g. length-cutoff
    detection) but ignored in this version; the framework loop still surfaces
    it on ``RoundEndCtx.finish_reason`` for hook consumers.
    """
    if budget_limits is not None:
        if budget_state.tokens_used > budget_limits.max_tokens:
            return CompletionDecision(
                status="budget_exhausted",
                reason=(
                    f"tokens_used={budget_state.tokens_used} "
                    f"> max_tokens={budget_limits.max_tokens}"
                ),
                limit_kind="tokens",
            )
        if budget_state.steps_used > budget_limits.max_steps:
            return CompletionDecision(
                status="budget_exhausted",
                reason=(
                    f"steps_used={budget_state.steps_used} "
                    f"> max_steps={budget_limits.max_steps}"
                ),
                limit_kind="steps",
            )
        if budget_state.tool_calls_used > budget_limits.max_tool_calls:
            return CompletionDecision(
                status="budget_exhausted",
                reason=(
                    f"tool_calls_used={budget_state.tool_calls_used} "
                    f"> max_tool_calls={budget_limits.max_tool_calls}"
                ),
                limit_kind="tool_calls",
            )

    calls = tool_calls or []
    results = tool_results or []

    if not calls:
        return CompletionDecision(
            status="completed",
            reason="no_tool_calls",
        )

    for idx, result in enumerate(results):
        if result.get("terminal") is True:
            if result.get("success") is False:
                return CompletionDecision(
                    status="failed",
                    reason="terminal_failure",
                    terminal_index=idx,
                )
            return CompletionDecision(
                status="completed",
                reason="terminal_result",
                terminal_index=idx,
            )

    if results and all(is_terminal_result(r) for r in results):
        return CompletionDecision(
            status="failed",
            reason="all_results_terminal",
        )

    return CompletionDecision(
        status="executing",
        reason="has_pending_tool_calls",
    )
