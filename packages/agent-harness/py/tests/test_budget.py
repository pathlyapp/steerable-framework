from __future__ import annotations

from steerable_agent_harness import budget


def _limits(**overrides) -> budget.BudgetLimit:
    base = {"max_tokens": 1000, "max_steps": 10, "max_tool_calls": 5}
    base.update(overrides)
    return budget.BudgetLimit(**base)


def test_budget_consume_returns_new_state() -> None:
    state = budget.BudgetState()
    next_state, exhausted = budget.consume_budget(
        state,
        _limits(),
        tokens=200,
        step=True,
        tool_call=True,
    )
    assert next_state.tokens_used == 200
    assert next_state.steps_used == 1
    assert next_state.tool_calls_used == 1
    assert exhausted is False
    assert state.tokens_used == 0  # original state untouched


def test_budget_token_overflow_exhausted() -> None:
    state = budget.BudgetState(tokens_used=900)
    _, exhausted = budget.consume_budget(state, _limits(), tokens=200)
    assert exhausted is True


def test_budget_step_overflow_exhausted() -> None:
    state = budget.BudgetState(steps_used=10)
    _, exhausted = budget.consume_budget(state, _limits(), step=True)
    assert exhausted is True


def test_budget_tool_call_overflow_exhausted() -> None:
    state = budget.BudgetState(tool_calls_used=5)
    _, exhausted = budget.consume_budget(state, _limits(), tool_call=True)
    assert exhausted is True


def test_negative_tokens_clamp_to_zero() -> None:
    state = budget.BudgetState(tokens_used=10)
    next_state, exhausted = budget.consume_budget(state, _limits(), tokens=-50)
    assert next_state.tokens_used == 10
    assert exhausted is False


def test_idle_call_does_not_increment() -> None:
    state = budget.BudgetState(tokens_used=5, steps_used=1, tool_calls_used=1)
    next_state, exhausted = budget.consume_budget(state, _limits())
    assert next_state == state
    assert exhausted is False


def test_budget_golden(assert_golden) -> None:
    state = budget.BudgetState()
    timeline = []
    for i in range(3):
        state, exhausted = budget.consume_budget(
            state,
            _limits(max_tokens=400),
            tokens=150,
            step=True,
            tool_call=(i % 2 == 0),
        )
        timeline.append(
            {
                "step": i + 1,
                "tokens_used": state.tokens_used,
                "steps_used": state.steps_used,
                "tool_calls_used": state.tool_calls_used,
                "exhausted": exhausted,
            }
        )
    assert_golden("budget_timeline", timeline)
