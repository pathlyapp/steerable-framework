from __future__ import annotations

from pathlib import Path

import yaml

from steerable_agent_harness.budget import (
    BudgetLimit,
    BudgetState,
    consume_budget,
)


def test_budget_conformance_case() -> None:
    case_path = (
        Path(__file__).resolve().parents[1] / "cases" / "budget" / "consume.yaml"
    )
    case = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    limits = BudgetLimit(
        max_tokens=case["limits"]["maxTokens"],
        max_steps=case["limits"]["maxSteps"],
        max_tool_calls=case["limits"]["maxToolCalls"],
    )
    state = BudgetState()
    actual: list[dict[str, object]] = []
    for op in case["ops"]:
        state, exhausted = consume_budget(
            state,
            limits,
            tokens=op.get("tokens", 0),
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
