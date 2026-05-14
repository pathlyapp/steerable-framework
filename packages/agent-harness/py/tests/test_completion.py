from __future__ import annotations

from steerable_agent_harness import completion


def test_terminal_when_explicit_flag() -> None:
    assert completion.is_terminal_result({"terminal": True}) is True


def test_terminal_when_failure_without_followup() -> None:
    assert completion.is_terminal_result({"success": False}) is True


def test_not_terminal_when_failure_needs_followup() -> None:
    assert (
        completion.is_terminal_result({"success": False, "needsFollowup": True})
        is False
    )


def test_not_terminal_for_pure_success() -> None:
    assert completion.is_terminal_result({"success": True}) is False


def test_empty_or_none_not_terminal() -> None:
    assert completion.is_terminal_result(None) is False
    assert completion.is_terminal_result({}) is False


def test_completion_golden(assert_golden) -> None:
    cases = [
        {"success": True},
        {"success": False},
        {"success": False, "needsFollowup": True},
        {"terminal": True},
        {"success": False, "terminal": True},
        {},
        None,
    ]
    payload = [
        {"input": case, "is_terminal": completion.is_terminal_result(case)}
        for case in cases
    ]
    assert_golden("completion_decisions", payload)
