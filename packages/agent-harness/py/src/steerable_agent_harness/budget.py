from __future__ import annotations

import math
from dataclasses import dataclass

#: Default for ``BudgetLimit.cached_token_weight``.
DEFAULT_CACHED_TOKEN_WEIGHT = 0.1


@dataclass(slots=True)
class BudgetLimit:
    max_tokens: int
    max_steps: int
    max_tool_calls: int
    #: Fraction of a normal token charged for prompt tokens the provider
    #: served from its cache. The token budget is a cost proxy, and a cache
    #: hit is priced well below a miss (~0.1x for Anthropic cache-read and
    #: DeepSeek cache-hit, ~0.5x for OpenAI cached input). An agentic turn
    #: re-sends a mostly identical prefix every round, so charging hits at
    #: par exhausts the budget several times sooner than the run's cost
    #: warrants. Hosts on providers with pricier caches raise it. Inert
    #: unless the caller reports ``cached_tokens``.
    cached_token_weight: float = DEFAULT_CACHED_TOKEN_WEIGHT


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
    cached_tokens: int = 0,
    step: bool = False,
    tool_call: bool = False,
) -> tuple[BudgetState, bool]:
    """Charge one request against the budget, returning the new state.

    ``tokens`` is the request's total usage; ``cached_tokens`` is the subset
    of it the provider served from its prompt cache, discounted by
    ``limits.cached_token_weight``. The discount is floored to an integer
    before subtraction so this stays bit-identical to the TypeScript port
    (conformance case ``cases/budget/cached.yaml``); a provider that reports
    more cached tokens than total clamps to a zero charge rather than
    refunding budget.
    """

    billed_tokens = max(tokens, 0)
    cached = max(cached_tokens, 0)
    if cached:
        billed_tokens = max(
            billed_tokens - cached + math.floor(cached * limits.cached_token_weight),
            0,
        )
    next_state = BudgetState(
        tokens_used=state.tokens_used + billed_tokens,
        steps_used=state.steps_used + (1 if step else 0),
        tool_calls_used=state.tool_calls_used + (1 if tool_call else 0),
    )
    exhausted = (
        next_state.tokens_used > limits.max_tokens
        or next_state.steps_used > limits.max_steps
        or next_state.tool_calls_used > limits.max_tool_calls
    )
    return next_state, exhausted
