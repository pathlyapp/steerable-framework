"""Approval algebra: 8-variant decisions, three persistence scopes, Approver seam.

Reference: codex-rs ``protocol/src/approvals.rs`` ``ReviewDecision`` — the same
algebra: allow/deny across request/session/durable scopes, ``Denied{reason}``
fed back to the model as a tool result while the run continues, ``Abort``
ending the turn, and a fail-closed timeout variant. The framework generalizes
codex's policy-amendment variants into the durable scope (``allow_always`` /
``deny_always``) keyed by category.

Semantics of the three scopes:

- **request** (``*_once``): applies to this call only; nothing is cached.
- **session** (``*_for_session``): cached per category in a
  ``SessionApprovalCache``; the host defines "session" by the cache instance's
  lifetime (one chat, one sidecar process, …).
- **durable** (``*_always``): persisted per category in an ``ApprovalStore``
  and applied across sessions.

``ApprovalExecutor`` is a ``ToolExecutor`` decorator, so the algebra works in
front of any dispatch path — in-process router, host reverse channel, or MCP
invoker — instead of being baked into one registry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from steerable_agent_harness.policy import ToolMode, decide_tool_mode
from steerable_agent_protocol.generated import ToolCall, ToolResult

from .errors import ApprovalAborted

if TYPE_CHECKING:
    from .loop import LoopContext, ToolExecutor

logger = logging.getLogger(__name__)

__all__ = [
    "APPROVAL_KINDS",
    "ApprovalDecision",
    "ApprovalExecutor",
    "ApprovalKind",
    "ApprovalRequest",
    "ApprovalStore",
    "Approver",
    "AutoApprover",
    "InMemoryApprovalStore",
    "JsonApprovalStore",
    "SessionApprovalCache",
]

#: The 8 decision variants. Codex's ``ApprovedExecpolicyAmendment`` /
#: ``ApprovedMcpPolicyAmendment`` / ``NetworkPolicyAmendment`` are codex's
#: policy machinery; the framework's durable scope covers their semantics
#: without the amendment payloads.
ApprovalKind = Literal[
    "allow_once",
    "allow_for_session",
    "allow_always",
    "deny_once",
    "deny_for_session",
    "deny_always",
    "abort",
    "timed_out",
]

_ALLOW_KINDS = frozenset({"allow_once", "allow_for_session", "allow_always"})
_SESSION_KINDS = frozenset({"allow_for_session", "deny_for_session"})
_DURABLE_KINDS = frozenset({"allow_always", "deny_always"})
#: All valid kinds — wire decoders validate against this set.
APPROVAL_KINDS = frozenset(
    _ALLOW_KINDS | _SESSION_KINDS | _DURABLE_KINDS | {"abort", "timed_out"}
)


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """One approval verdict. ``reason`` is model-visible on deny/abort."""

    kind: ApprovalKind
    reason: str = ""

    @property
    def allowed(self) -> bool:
        """Whether the call may execute (any allow variant)."""
        return self.kind in _ALLOW_KINDS


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """What the approver decides on.

    ``category`` is the persistence key for session/durable variants — all
    calls sharing a category share a cached decision. It defaults to the tool
    name; hosts with parameterized risk (e.g. a shell tool) can pass a
    resolver that categorizes by command class instead.
    """

    tool_name: str
    arguments: dict[str, Any]
    mode: ToolMode
    category: str
    round_index: int = 0


@runtime_checkable
class Approver(Protocol):
    """The approval seam: given a request, return a decision.

    Implementations must not raise for ordinary denials — return a deny
    variant. Raising is reserved for infrastructure failure and surfaces as a
    tool error, not a denial.
    """

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision: ...


class AutoApprover:
    """Headless approver: never blocks, decides by tool mode.

    Modes in ``allow_modes`` auto-approve once; everything else auto-denies
    once with ``deny_reason`` — per-category automatic denial is what keeps a
    headless run from hanging on a prompt nobody will answer.
    """

    def __init__(
        self,
        *,
        allow_modes: tuple[ToolMode, ...] = ("read", "safe_write"),
        deny_reason: str = "no interactive approver is attached (headless run)",
    ) -> None:
        self._allow_modes = frozenset(allow_modes)
        self._deny_reason = deny_reason

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        if request.mode in self._allow_modes:
            return ApprovalDecision("allow_once")
        return ApprovalDecision("deny_once", self._deny_reason)


class SessionApprovalCache:
    """Session scope: per-category decisions for the cache instance's lifetime."""

    def __init__(self) -> None:
        self._decisions: dict[str, ApprovalKind] = {}

    def get(self, category: str) -> ApprovalKind | None:
        return self._decisions.get(category)

    def put(self, category: str, kind: ApprovalKind) -> None:
        if kind not in _SESSION_KINDS:
            raise ValueError(f"not a session-scope kind: {kind}")
        self._decisions[category] = kind


@runtime_checkable
class ApprovalStore(Protocol):
    """Durable scope: per-category decisions persisted across sessions."""

    def load(self) -> dict[str, ApprovalKind]:
        """All persisted ``category → kind`` entries (empty on first use)."""
        ...

    def put(self, category: str, kind: ApprovalKind) -> None:
        """Persist one entry, replacing any existing decision for the category."""
        ...


class InMemoryApprovalStore:
    """Non-durable ``ApprovalStore`` for tests and ephemeral hosts."""

    def __init__(self) -> None:
        self._decisions: dict[str, ApprovalKind] = {}

    def load(self) -> dict[str, ApprovalKind]:
        return dict(self._decisions)

    def put(self, category: str, kind: ApprovalKind) -> None:
        if kind not in _DURABLE_KINDS:
            raise ValueError(f"not a durable-scope kind: {kind}")
        self._decisions[category] = kind


class JsonApprovalStore:
    """File-backed durable store: one JSON object ``{category: kind}``.

    Writes are atomic (tmp file + rename) so a crash mid-write cannot leave a
    truncated file that would fail every future load.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load(self) -> dict[str, ApprovalKind]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt store must fail closed, not crash the run: treating it
            # as empty re-asks the approver instead of auto-allowing.
            logger.warning("approval store %s unreadable (%s); ignoring", self._path, exc)
            return {}
        return {
            str(category): kind
            for category, kind in raw.items()
            if kind in _DURABLE_KINDS
        }

    def put(self, category: str, kind: ApprovalKind) -> None:
        if kind not in _DURABLE_KINDS:
            raise ValueError(f"not a durable-scope kind: {kind}")
        decisions = self.load()
        decisions[category] = kind
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(decisions, handle, ensure_ascii=False, indent=1)
            os.replace(tmp, self._path)
        except BaseException:
            os.unlink(tmp)
            raise


def _default_resolver(call: ToolCall, ctx: LoopContext) -> ApprovalRequest:
    """Classify by tool name via the harness policy table; category = name."""
    return ApprovalRequest(
        tool_name=call.name,
        arguments=call.arguments or {},
        mode=decide_tool_mode(call.name),
        category=call.name,
        round_index=ctx.round_index,
    )


class ApprovalExecutor:
    """``ToolExecutor`` decorator enforcing the approval algebra.

    Resolution order per call: durable store → session cache → approver.
    Session/durable variants are persisted on first decision and short-circuit
    later calls in the same category. Deny variants return a failed
    ``ToolResult`` (the model sees ``Denied{reason}`` and the run continues);
    ``abort`` raises ``ApprovalAborted``; ``timed_out`` fails closed as a
    denial that keeps the variant name for observability.

    On allow, ``ctx.consent_granted`` is set so a downstream
    ``RouterToolExecutor``'s ``require_consent`` gate recognizes the approval
    instead of double-gating. Every call still passes through this decorator
    first, so the bridge cannot bypass approval.
    """

    def __init__(
        self,
        inner: ToolExecutor,
        approver: Approver,
        *,
        session: SessionApprovalCache | None = None,
        store: ApprovalStore | None = None,
        resolve: Any | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self._inner = inner
        self._approver = approver
        self._session = session
        self._store = store
        self._resolve = resolve or _default_resolver
        self._timeout_s = timeout_s
        self._durable: dict[str, ApprovalKind] | None = None

    def concurrency_safe(self, call: ToolCall) -> bool:
        check = getattr(self._inner, "concurrency_safe", None)
        return bool(check is not None and check(call))

    async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
        request = self._resolve(call, ctx)
        decision = self._stored_decision(request)
        if decision is None:
            decision = await self._ask(request)
            self._persist(request, decision)

        if decision.kind == "abort":
            raise ApprovalAborted(
                decision.reason or f"tool call '{request.tool_name}' aborted by approval"
            )
        if not decision.allowed:
            reason = decision.reason or "denied by approval policy"
            return ToolResult(
                success=False,
                error="approval_denied",
                needsFollowup=True,
                data={
                    "approval": decision.kind,
                    "category": request.category,
                    "reason": reason,
                    "message": (
                        f"Tool call '{request.tool_name}' was denied "
                        f"({decision.kind}): {reason}"
                    ),
                },
            )
        ctx.consent_granted = True
        return await self._inner.execute(call, ctx)

    def _stored_decision(self, request: ApprovalRequest) -> ApprovalDecision | None:
        """Durable wins over session: it is the stronger commitment."""
        durable = self._durable_decisions().get(request.category)
        if durable is not None:
            return ApprovalDecision(
                durable, f"persisted decision for category '{request.category}'"
            )
        if self._session is not None:
            session_kind = self._session.get(request.category)
            if session_kind is not None:
                return ApprovalDecision(
                    session_kind,
                    f"session decision for category '{request.category}'",
                )
        return None

    def _durable_decisions(self) -> dict[str, ApprovalKind]:
        if self._store is None:
            return {}
        if self._durable is None:
            self._durable = self._store.load()
        return self._durable

    async def _ask(self, request: ApprovalRequest) -> ApprovalDecision:
        if self._timeout_s is None:
            return await self._approver.approve(request)
        try:
            return await asyncio.wait_for(
                self._approver.approve(request), timeout=self._timeout_s
            )
        except (TimeoutError, asyncio.TimeoutError):
            # Fail closed, keeping the variant for observability (codex's
            # TimedOut semantics: no decision arrived, do not execute).
            return ApprovalDecision(
                "timed_out", f"approval request timed out after {self._timeout_s}s"
            )

    def _persist(self, request: ApprovalRequest, decision: ApprovalDecision) -> None:
        if decision.kind in _SESSION_KINDS and self._session is not None:
            self._session.put(request.category, decision.kind)
        elif decision.kind in _DURABLE_KINDS:
            if self._store is not None:
                self._store.put(request.category, decision.kind)
                if self._durable is not None:
                    self._durable[request.category] = decision.kind
            elif self._session is not None:
                # A host approver returned a durable variant without wiring a
                # store — degrade to session scope loudly rather than silently
                # dropping the persistence the user asked for.
                logger.warning(
                    "no ApprovalStore configured; degrading %s for category %r "
                    "to session scope",
                    decision.kind,
                    request.category,
                )
                self._session.put(
                    request.category,
                    "allow_for_session"
                    if decision.kind == "allow_always"
                    else "deny_for_session",
                )
