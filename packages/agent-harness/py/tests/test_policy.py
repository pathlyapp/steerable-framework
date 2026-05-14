from __future__ import annotations

import pytest

from steerable_agent_harness import policy


@pytest.mark.parametrize(
    "tool_name,expected",
    [
        ("get_user", "read"),
        ("list_files", "read"),
        ("read_file", "read"),
        ("create_event", "safe_write"),
        ("update_chat", "safe_write"),
        ("set_config", "safe_write"),
        ("write_file", "safe_write"),
        ("apply_patch", "safe_write"),
        ("delete_event", "destructive"),
        ("drop_table", "destructive"),
        ("remove_user", "destructive"),
        ("destroy_session", "destructive"),
        ("compute_score", "other"),
        ("orchestrate_steps", "other"),
        ("", "other"),
    ],
)
def test_decide_tool_mode(tool_name: str, expected: str) -> None:
    assert policy.decide_tool_mode(tool_name) == expected


def test_decide_tool_mode_is_case_insensitive() -> None:
    assert policy.decide_tool_mode("DELETE_USER") == "destructive"
    assert policy.decide_tool_mode("Get_User") == "read"


def test_policy_decision_dataclass_round_trip() -> None:
    decision = policy.PolicyDecision(allowed=True, tool_mode="read", reason="auto")
    assert decision.allowed is True
    assert decision.tool_mode == "read"
    assert decision.reason == "auto"


def test_policy_decision_golden(assert_golden) -> None:
    samples = [
        ("get_x", policy.decide_tool_mode("get_x")),
        ("create_x", policy.decide_tool_mode("create_x")),
        ("delete_x", policy.decide_tool_mode("delete_x")),
        ("frobnicate", policy.decide_tool_mode("frobnicate")),
    ]
    payload = {name: mode for name, mode in samples}
    assert_golden("policy_decisions", payload)
