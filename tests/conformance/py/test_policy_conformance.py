from __future__ import annotations

from pathlib import Path

import yaml

from steerable_agent_harness.policy import decide_tool_mode


def test_policy_conformance_case() -> None:
    case_path = (
        Path(__file__).resolve().parents[1]
        / "cases"
        / "policy"
        / "decide_tool_mode.yaml"
    )
    case = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    actual = [decide_tool_mode(name) for name in case["inputs"]]
    assert actual == case["expected"]
