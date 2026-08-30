"""Approval policy rules: pattern-matched auto-decisions orthogonal to the
8-variant lattice (W2.4).

Reference: codex execpolicy — durable rules mapping command patterns to
allow/deny verdicts, consulted before the interactive prompt. The lattice's
category caches answer "this KIND of call, decided before"; policy rules
answer "calls matching this PATTERN, decided in advance" — a shell category
covers every command, while a rule can carve out ``git status`` precisely.

Resolution order inside ``ApprovalExecutor`` is unchanged (durable store →
session cache → approver); ``PolicyApprover`` sits at the approver seam, so
rules are consulted after the lattice's own caches and before the host
prompt. Rule hits return request-scoped variants (``allow_once`` /
``deny_once``): the rule itself is the durable grant, so the lattice must
not double-record it into a category cache.

Amendments (W2.4.2): a host approval reply may carry an ``amendment``
payload — "allow, and keep allowing commands like this". The sidecar turns
it into a rule in the durable policy store; the user is not re-asked.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .approval import ApprovalDecision, ApprovalRequest, Approver

logger = logging.getLogger(__name__)

__all__ = [
    "ApprovalPolicy",
    "ApprovalRule",
    "JsonApprovalPolicyStore",
    "PolicyApprover",
    "rule_from_amendment",
]

RuleDecision = Literal["allow", "deny"]
_RULE_DECISIONS = frozenset({"allow", "deny"})

#: Conventional arguments key holding the shell command string (mirrors the
#: sidecar's default ``commandArg``).
_DEFAULT_COMMAND_ARG = "command"


@dataclass(frozen=True, slots=True)
class ApprovalRule:
    """One pattern → verdict rule.

    ``tool`` is an exact tool name. ``command_prefix`` is an argv token
    prefix matched against the call's command string (shlex-split); empty
    means "every call of this tool". A rule with a prefix never matches a
    call whose command is missing or unparseable — fail closed, the inner
    approver decides instead.
    """

    tool: str
    decision: RuleDecision
    command_prefix: tuple[str, ...] = ()
    command_arg: str = _DEFAULT_COMMAND_ARG

    def __post_init__(self) -> None:
        if not self.tool:
            raise ValueError("rule tool must be non-empty")
        if self.decision not in _RULE_DECISIONS:
            raise ValueError(f"rule decision must be allow|deny, got {self.decision!r}")

    def matches(self, request: ApprovalRequest) -> bool:
        if request.tool_name != self.tool:
            return False
        if not self.command_prefix:
            return True
        raw = request.arguments.get(self.command_arg)
        if not isinstance(raw, str) or not raw.strip():
            return False
        try:
            argv = shlex.split(raw)
        except ValueError:  # unbalanced quotes etc. — no match, fail closed
            return False
        prefix = list(self.command_prefix)
        return argv[: len(prefix)] == prefix

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "decision": self.decision,
            "commandPrefix": list(self.command_prefix),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalRule:
        tool = data.get("tool")
        decision = data.get("decision")
        prefix = data.get("commandPrefix") or []
        if not isinstance(tool, str) or not isinstance(prefix, list):
            raise ValueError(f"invalid rule entry: {data!r}")
        return cls(
            tool=tool,
            decision=decision,  # validated in __post_init__
            command_prefix=tuple(str(token) for token in prefix),
        )


@dataclass(slots=True)
class ApprovalPolicy:
    """Ordered rule list; the first matching rule decides."""

    rules: list[ApprovalRule] = field(default_factory=list)

    def decide(self, request: ApprovalRequest) -> ApprovalDecision | None:
        for rule in self.rules:
            if rule.matches(request):
                pattern = (
                    " ".join(rule.command_prefix) if rule.command_prefix else "*"
                )
                reason = f"policy rule: {rule.tool} {pattern} → {rule.decision}"
                return ApprovalDecision(
                    "allow_once" if rule.decision == "allow" else "deny_once",
                    reason,
                )
        return None

    def add(self, rule: ApprovalRule) -> None:
        """Append a rule. An identical existing rule is a no-op (amendments
        are idempotent — re-approving the same pattern must not grow the
        file)."""
        if rule not in self.rules:
            self.rules.append(rule)


class JsonApprovalPolicyStore:
    """File-backed durable policy: one JSON object ``{"version", "rules"}``.

    Writes are atomic (tmp file + rename), mirroring ``JsonApprovalStore`` —
    a crash mid-write must not corrupt the policy every future run loads.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load(self) -> ApprovalPolicy:
        if not self._path.exists():
            return ApprovalPolicy()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            rules = [ApprovalRule.from_dict(r) for r in data.get("rules") or []]
        except (ValueError, KeyError, AttributeError) as exc:
            # A corrupt policy fails closed: no rules, so every call falls
            # through to the interactive approver — never an auto-allow.
            logger.warning("ignoring unreadable approval policy %s: %s", self._path, exc)
            return ApprovalPolicy()
        return ApprovalPolicy(rules)

    def save(self, policy: ApprovalPolicy) -> None:
        payload = {
            "version": 1,
            "rules": [rule.to_dict() for rule in policy.rules],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=self._path.name, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def add_rule(self, rule: ApprovalRule) -> None:
        """Load → add → save. The policy is small (user-authored rules), so
        read-modify-write per amendment is fine and keeps the file the single
        source of truth across processes."""
        policy = self.load()
        policy.add(rule)
        self.save(policy)


class PolicyApprover:
    """``Approver`` decorator consulting the policy before the inner approver.

    A rule hit short-circuits (no host round-trip); a miss delegates. The
    inner approver is typically ``HostApprover`` (interactive) or
    ``AutoApprover`` (headless) — the policy composes with either.
    """

    def __init__(self, inner: Approver, policy: ApprovalPolicy) -> None:
        self._inner = inner
        self._policy = policy

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        decision = self._policy.decide(request)
        if decision is not None:
            return decision
        return await self._inner.approve(request)


def rule_from_amendment(
    request: ApprovalRequest, amendment: Any
) -> ApprovalRule | None:
    """Decode a host reply's ``amendment`` payload into a rule (W2.4.2).

    Wire shape::

        {"decision": "allow" | "deny", "commandPrefix": ["git", "status"]?}

    The rule's tool is the approved call's tool — an amendment can only
    widen/narrow the pattern, never retarget another tool. Invalid payloads
    return ``None`` (the decision itself still stands; only the persistence
    is dropped), never raise into the approval path.
    """
    if not isinstance(amendment, dict):
        return None
    decision = amendment.get("decision")
    if decision not in _RULE_DECISIONS:
        logger.warning("ignoring approval amendment with bad decision: %r", amendment)
        return None
    prefix_raw = amendment.get("commandPrefix") or []
    if not isinstance(prefix_raw, list) or not all(
        isinstance(t, str) for t in prefix_raw
    ):
        logger.warning("ignoring approval amendment with bad commandPrefix: %r", amendment)
        return None
    try:
        return ApprovalRule(
            tool=request.tool_name,
            decision=decision,
            command_prefix=tuple(prefix_raw),
        )
    except ValueError:
        return None
