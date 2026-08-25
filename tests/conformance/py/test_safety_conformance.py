from __future__ import annotations

from pathlib import Path

import yaml
from steerable_agent_harness import classify_shell_command


def test_safety_conformance_case() -> None:
    case_path = (
        Path(__file__).resolve().parents[1]
        / "cases"
        / "safety"
        / "classify_shell_command.yaml"
    )
    case = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    actual = [
        {
            "severity": result.severity,
            "matchedRules": result.matched_rules,
        }
        for result in (classify_shell_command(cmd) for cmd in case["inputs"])
    ]
    expected = [
        {"severity": e["severity"], "matchedRules": e["matchedRules"]}
        for e in case["expected"]
    ]
    assert actual == expected
