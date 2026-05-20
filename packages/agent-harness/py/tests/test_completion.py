from __future__ import annotations

from steerable_agent_harness import (
    BudgetLimit,
    BudgetState,
    CompletionDecision,
    completion,
    decide_completion,
)


# ---------------------------------------------------------------------------
# is_terminal_result (kept for back compat)
# ---------------------------------------------------------------------------


def test_terminal_when_explicit_flag() -> None:
    assert completion.is_terminal_result({"terminal": True}) is True


def test_terminal_when_failure_without_followup() -> None:
    assert completion.is_terminal_result({"success": False}) is True


def test_not_terminal_when_failure_needs_followup() -> None:
    assert (
        completion.is_terminal_result({"success": False, "needsFollowup": True})
        is False
    )


def test_not_terminal_for_pure_success() -> None:
    assert completion.is_terminal_result({"success": True}) is False


def test_empty_or_none_not_terminal() -> None:
    assert completion.is_terminal_result(None) is False
    assert completion.is_terminal_result({}) is False


def test_completion_golden(assert_golden) -> None:
    cases = [
        {"success": True},
        {"success": False},
        {"success": False, "needsFollowup": True},
        {"terminal": True},
        {"success": False, "terminal": True},
        {},
        None,
    ]
    payload = [
        {"input": case, "is_terminal": completion.is_terminal_result(case)}
        for case in cases
    ]
    assert_golden("completion_decisions", payload)


# ---------------------------------------------------------------------------
# decide_completion
# ---------------------------------------------------------------------------


_BIG_BUDGET = BudgetLimit(max_tokens=1_000_000, max_steps=100, max_tool_calls=100)


def _fresh_state(**overrides: int) -> BudgetState:
    return BudgetState(**overrides)


def test_decide_no_tool_calls_means_completed() -> None:
    d = decide_completion(
        tool_calls=[],
        tool_results=[],
        budget_state=_fresh_state(),
        budget_limits=_BIG_BUDGET,
    )
    assert d.status == "completed"
    assert d.reason == "no_tool_calls"
    assert d.limit_kind is None


def test_decide_tool_calls_without_results_means_executing() -> None:
    """Should not happen in practice, but guards the decision tree."""
    d = decide_completion(
        tool_calls=[{"id": "c1", "name": "x", "arguments": {}}],
        tool_results=[],
        budget_state=_fresh_state(),
        budget_limits=_BIG_BUDGET,
    )
    assert d.status == "executing"


def test_decide_terminal_success_marks_completed_with_index() -> None:
    d = decide_completion(
        tool_calls=[{"id": "c1"}, {"id": "c2"}],
        tool_results=[
            {"success": True, "terminal": False},
            {"success": True, "terminal": True},
        ],
        budget_state=_fresh_state(),
        budget_limits=_BIG_BUDGET,
    )
    assert d.status == "completed"
    assert d.reason == "terminal_result"
    assert d.terminal_index == 1


def test_decide_terminal_failure_marks_failed_with_index() -> None:
    d = decide_completion(
        tool_calls=[{"id": "c1"}],
        tool_results=[{"success": False, "terminal": True}],
        budget_state=_fresh_state(),
        budget_limits=_BIG_BUDGET,
    )
    assert d.status == "failed"
    assert d.reason == "terminal_failure"
    assert d.terminal_index == 0


def test_decide_all_results_terminal_marks_failed() -> None:
    """Every result is a non-followup failure → loop gives up."""
    d = decide_completion(
        tool_calls=[{"id": "c1"}, {"id": "c2"}],
        tool_results=[
            {"success": False, "error": "auth"},
            {"success": False, "error": "timeout"},
        ],
        budget_state=_fresh_state(),
        budget_limits=_BIG_BUDGET,
    )
    assert d.status == "failed"
    assert d.reason == "all_results_terminal"


def test_decide_followup_failures_keep_loop_executing() -> None:
    """Errors that request a follow-up are self-healing, not terminal."""
    d = decide_completion(
        tool_calls=[{"id": "c1"}],
        tool_results=[{"success": False, "needsFollowup": True}],
        budget_state=_fresh_state(),
        budget_limits=_BIG_BUDGET,
    )
    assert d.status == "executing"


def test_decide_budget_tokens_exhausted() -> None:
    d = decide_completion(
        tool_calls=[{"id": "c1"}],
        tool_results=[{"success": True}],
        budget_state=_fresh_state(tokens_used=120_001),
        budget_limits=BudgetLimit(max_tokens=120_000, max_steps=10, max_tool_calls=10),
    )
    assert d.status == "budget_exhausted"
    assert d.limit_kind == "tokens"
    assert "max_tokens=120000" in d.reason


def test_decide_budget_steps_exhausted() -> None:
    d = decide_completion(
        tool_calls=[{"id": "c1"}],
        tool_results=[{"success": True}],
        budget_state=_fresh_state(steps_used=11),
        budget_limits=BudgetLimit(max_tokens=100, max_steps=10, max_tool_calls=10),
    )
    assert d.status == "budget_exhausted"
    assert d.limit_kind == "steps"


def test_decide_budget_tool_calls_exhausted() -> None:
    d = decide_completion(
        tool_calls=[{"id": "c1"}],
        tool_results=[{"success": True}],
        budget_state=_fresh_state(tool_calls_used=21),
        budget_limits=BudgetLimit(max_tokens=100, max_steps=10, max_tool_calls=20),
    )
    assert d.status == "budget_exhausted"
    assert d.limit_kind == "tool_calls"


def test_decide_budget_priority_over_terminal_result() -> None:
    """Budget exhaustion is reported even if a terminal result is present:
    we want to flag the runaway, not hide it behind a happy stop."""
    d = decide_completion(
        tool_calls=[{"id": "c1"}],
        tool_results=[{"success": True, "terminal": True}],
        budget_state=_fresh_state(tokens_used=999_999),
        budget_limits=BudgetLimit(max_tokens=100, max_steps=10, max_tool_calls=10),
    )
    assert d.status == "budget_exhausted"
    assert d.limit_kind == "tokens"


def test_decide_no_budget_limits_skips_budget_branch() -> None:
    """``budget_limits=None`` means "don't check budget" — the rest of the
    decision tree must still work."""
    d = decide_completion(
        tool_calls=[],
        tool_results=[],
        budget_state=_fresh_state(tokens_used=999_999_999),  # ridiculous
        budget_limits=None,
    )
    assert d.status == "completed"
    assert d.reason == "no_tool_calls"


def test_decide_decision_to_dict_round_trip() -> None:
    d = CompletionDecision(
        status="failed",
        reason="terminal_failure",
        terminal_index=2,
    )
    assert d.to_dict() == {
        "status": "failed",
        "reason": "terminal_failure",
        "limit_kind": None,
        "terminal_index": 2,
    }


def test_decide_none_inputs_are_safe() -> None:
    """Defensive: tool_calls=None and tool_results=None must not crash."""
    d = decide_completion(
        tool_calls=None,
        tool_results=None,
        budget_state=_fresh_state(),
        budget_limits=_BIG_BUDGET,
    )
    assert d.status == "completed"
