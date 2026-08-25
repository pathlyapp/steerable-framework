from __future__ import annotations

from pathlib import Path

import yaml

from steerable_agent_harness.completion import is_terminal_result


def test_completion_conformance_case() -> None:
    case_path = (
        Path(__file__).resolve().parents[1]
        / "cases"
        / "completion"
        / "is_terminal_result.yaml"
    )
    case = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    actual = [is_terminal_result(result) for result in case["inputs"]]
    assert actual == case["expected"]
