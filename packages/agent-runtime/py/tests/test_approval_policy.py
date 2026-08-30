"""W2.4: pattern approval rules (execpolicy counterpart) + amendment decode."""

from __future__ import annotations

import json
from typing import Any

import pytest
from steerable_agent_runtime import (
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalRule,
    JsonApprovalPolicyStore,
    PolicyApprover,
    rule_from_amendment,
)


def _request(command: str | None = "git status", tool: str = "shell") -> ApprovalRequest:
    args: dict[str, Any] = {"command": command} if command is not None else {}
    return ApprovalRequest(
        tool_name=tool, arguments=args, mode="destructive", category=tool  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Rule matching
# ---------------------------------------------------------------------------


def test_prefix_rule_matches_argv_prefix() -> None:
    rule = ApprovalRule(tool="shell", decision="allow", command_prefix=("git", "status"))
    assert rule.matches(_request("git status"))
    assert rule.matches(_request("git status --short"))
    assert not rule.matches(_request("git push"))
    assert not rule.matches(_request("git"))  # shorter than the prefix
    assert not rule.matches(_request(tool="read_file"))  # wrong tool


def test_empty_prefix_matches_every_call_of_the_tool() -> None:
    rule = ApprovalRule(tool="shell", decision="deny")
    assert rule.matches(_request("rm -rf /"))
    assert rule.matches(_request(None))
    assert not rule.matches(_request(tool="read_file"))


def test_unparseable_command_never_matches_a_prefix_rule() -> None:
    rule = ApprovalRule(tool="shell", decision="allow", command_prefix=("git",))
    assert not rule.matches(_request('git "unbalanced'))
    assert not rule.matches(_request(""))
    assert not rule.matches(_request(None))


def test_rule_validation() -> None:
    with pytest.raises(ValueError, match="tool"):
        ApprovalRule(tool="", decision="allow")
    with pytest.raises(ValueError, match="decision"):
        ApprovalRule(tool="shell", decision="maybe")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Policy: ordered, first match wins
# ---------------------------------------------------------------------------


def test_policy_first_match_wins() -> None:
    policy = ApprovalPolicy(
        [
            ApprovalRule(tool="shell", decision="allow", command_prefix=("git",)),
            ApprovalRule(tool="shell", decision="deny"),
        ]
    )
    assert policy.decide(_request("git status")).kind == "allow_once"  # type: ignore[union-attr]
    assert policy.decide(_request("rm x")).kind == "deny_once"  # type: ignore[union-attr]
    assert policy.decide(_request(tool="read_file")) is None


def test_policy_add_is_idempotent() -> None:
    policy = ApprovalPolicy()
    rule = ApprovalRule(tool="shell", decision="allow", command_prefix=("git",))
    policy.add(rule)
    policy.add(rule)
    assert policy.rules == [rule]


# ---------------------------------------------------------------------------
# Store: durable round-trip, atomic write, corrupt file fails closed
# ---------------------------------------------------------------------------


def test_store_round_trip(tmp_path) -> None:
    store = JsonApprovalPolicyStore(tmp_path / "policy.json")
    store.add_rule(ApprovalRule(tool="shell", decision="allow", command_prefix=("git", "status")))
    store.add_rule(ApprovalRule(tool="shell", decision="deny", command_prefix=("rm",)))

    loaded = store.load()
    assert loaded.decide(_request("git status")).kind == "allow_once"  # type: ignore[union-attr]
    assert loaded.decide(_request("rm -rf /")).kind == "deny_once"  # type: ignore[union-attr]
    assert loaded.decide(_request("echo hi")) is None


def test_store_missing_file_is_empty_policy(tmp_path) -> None:
    store = JsonApprovalPolicyStore(tmp_path / "nope.json")
    assert store.load().rules == []


def test_store_corrupt_file_fails_closed(tmp_path) -> None:
    path = tmp_path / "policy.json"
    path.write_text("{not json", encoding="utf-8")
    store = JsonApprovalPolicyStore(path)
    policy = store.load()
    assert policy.rules == []  # no rules → everything falls to the approver
    assert policy.decide(_request("git status")) is None


# ---------------------------------------------------------------------------
# PolicyApprover: rule hit short-circuits, miss delegates
# ---------------------------------------------------------------------------


class _RecordingApprover:
    def __init__(self, decision: ApprovalDecision) -> None:
        self.decision = decision
        self.requests: list[ApprovalRequest] = []

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return self.decision


@pytest.mark.asyncio
async def test_policy_approver_short_circuits_on_rule_hit() -> None:
    inner = _RecordingApprover(ApprovalDecision("deny_once", "host says no"))
    policy = ApprovalPolicy(
        [ApprovalRule(tool="shell", decision="allow", command_prefix=("git",))]
    )
    approver = PolicyApprover(inner, policy)

    decision = await approver.approve(_request("git status"))
    assert decision.kind == "allow_once"
    assert inner.requests == []  # no host round-trip


@pytest.mark.asyncio
async def test_policy_approver_delegates_on_miss() -> None:
    inner = _RecordingApprover(ApprovalDecision("allow_once"))
    policy = ApprovalPolicy(
        [ApprovalRule(tool="shell", decision="deny", command_prefix=("rm",))]
    )
    approver = PolicyApprover(inner, policy)

    decision = await approver.approve(_request("git status"))
    assert decision.kind == "allow_once"
    assert len(inner.requests) == 1


# ---------------------------------------------------------------------------
# Amendment decode (W2.4.2)
# ---------------------------------------------------------------------------


def test_amendment_decodes_to_rule_scoped_to_the_approved_tool() -> None:
    rule = rule_from_amendment(
        _request("git status"),
        {"decision": "allow", "commandPrefix": ["git", "status"]},
    )
    assert rule is not None
    assert rule.tool == "shell"
    assert rule.decision == "allow"
    assert rule.command_prefix == ("git", "status")


def test_amendment_without_prefix_covers_the_whole_tool() -> None:
    rule = rule_from_amendment(_request(), {"decision": "deny"})
    assert rule is not None
    assert rule.command_prefix == ()


def test_invalid_amendments_return_none() -> None:
    assert rule_from_amendment(_request(), None) is None
    assert rule_from_amendment(_request(), "allow") is None
    assert rule_from_amendment(_request(), {"decision": "maybe"}) is None
    assert rule_from_amendment(_request(), {"decision": "allow", "commandPrefix": "git"}) is None
    assert rule_from_amendment(_request(), {"decision": "allow", "commandPrefix": [1]}) is None
