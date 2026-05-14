from __future__ import annotations

from pathlib import Path

import yaml

from steerable_agent_harness.retry import RetryPolicy, next_retry_delay_ms


def test_retry_conformance_case() -> None:
    case_path = (
        Path(__file__).resolve().parents[1]
        / "cases"
        / "retry"
        / "basic.yaml"
    )
    case = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    policy = RetryPolicy(
        max_attempts=case["policy"]["maxAttempts"],
        base_delay_ms=case["policy"]["baseDelayMs"],
        max_delay_ms=case["policy"]["maxDelayMs"],
        jitter=case["policy"]["jitter"],
    )
    actual = [next_retry_delay_ms(policy, a) for a in case["attempts"]]
    assert actual == case["expected"]
