from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class BudgetLimit:
    max_tokens: int
    max_steps: int
    max_tool_calls: int


@dataclass(slots=True)
class BudgetState:
    tokens_used: int = 0
    steps_used: int = 0
    tool_calls_used: int = 0


def consume_budget(
    state: BudgetState,
    limits: BudgetLimit,
    *,
    tokens: int = 0,
    step: bool = False,
    tool_call: bool = False,
) -> tuple[BudgetState, bool]:
    next_state = BudgetState(
        tokens_used=state.tokens_used + max(tokens, 0),
        steps_used=state.steps_used + (1 if step else 0),
        tool_calls_used=state.tool_calls_used + (1 if tool_call else 0),
    )
    exhausted = (
        next_state.tokens_used > limits.max_tokens
        or next_state.steps_used > limits.max_steps
        or next_state.tool_calls_used > limits.max_tool_calls
    )
    return next_state, exhausted
