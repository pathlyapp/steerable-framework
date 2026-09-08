from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from steerable_agent_harness.budget import (
    BudgetLimit,
    BudgetState,
    consume_budget,
)

_CASES = Path(__file__).resolve().parents[1] / "cases" / "budget"


def _run_case(name: str) -> None:
    case = yaml.safe_load((_CASES / name).read_text(encoding="utf-8"))
    limits_kwargs: dict[str, Any] = {
        "max_tokens": case["limits"]["maxTokens"],
        "max_steps": case["limits"]["maxSteps"],
        "max_tool_calls": case["limits"]["maxToolCalls"],
    }
    if "cachedTokenWeight" in case["limits"]:
        limits_kwargs["cached_token_weight"] = case["limits"]["cachedTokenWeight"]
    limits = BudgetLimit(**limits_kwargs)
    state = BudgetState()
    actual: list[dict[str, object]] = []
    for op in case["ops"]:
        state, exhausted = consume_budget(
            state,
            limits,
            tokens=op.get("tokens", 0),
            cached_tokens=op.get("cachedTokens", 0),
            step=op.get("step", False),
            tool_call=op.get("toolCall", False),
        )
        actual.append(
            {
                "tokensUsed": state.tokens_used,
                "stepsUsed": state.steps_used,
                "toolCallsUsed": state.tool_calls_used,
                "exhausted": exhausted,
            }
        )
    assert actual == case["expected"]


def test_budget_conformance_case() -> None:
    _run_case("consume.yaml")


def test_budget_conformance_cached_case() -> None:
    _run_case("cached.yaml")
