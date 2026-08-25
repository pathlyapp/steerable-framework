"""Compact trajectory recording and replay — framework home.

Ported from deeppath-api's `app/services/harness/replay.py` +
`execution_state.py` (and kept aligned with deeppath-agent's TS
`src/harness/replay.ts`). This is the compact layer only: `step_decision`
events fold back into a `HarnessExecutionState` via `reduce_execution_state`.

Contract (shared across api / agent / framework):
  * the only event ``type`` is ``"step_decision"``; unknown types are skipped
  * a step is deduplicated by ``(round, traceStepId)``
  * only these decision.status values drive ``status``:
    executing / waiting_user / waiting_approval / completed / failed /
    budget_exhausted  (``planning`` / ``verifying`` do NOT)
  * budgets.stepCount = number of deduped steps;
    budgets.toolErrorCount = sum of each step's toolErrorCount
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

ExecutionStatus = Literal[
    "planning",
    "executing",
    "verifying",
    "waiting_user",
    "waiting_approval",
    "completed",
    "failed",
    "budget_exhausted",
]

#: decision.status values that transition the execution state machine.
_STATUS_DRIVING = {
    "executing",
    "waiting_user",
    "waiting_approval",
    "completed",
    "failed",
    "budget_exhausted",
}

TRAJECTORY_KEY = "harnessTrajectory"
MAX_TRAJECTORY_EVENTS = 100


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    return f"hrun_{uuid.uuid4().hex}"


@dataclass
class ExecutionBudget:
    max_steps: int = 10
    max_tool_errors: int = 2
    step_count: int = 0
    tool_error_count: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ExecutionBudget:
        raw = data or {}
        return cls(
            max_steps=int(raw.get("maxSteps") or 10),
            max_tool_errors=int(raw.get("maxToolErrors") or 2),
            step_count=int(raw.get("stepCount") or 0),
            tool_error_count=int(raw.get("toolErrorCount") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "maxSteps": self.max_steps,
            "maxToolErrors": self.max_tool_errors,
            "stepCount": self.step_count,
            "toolErrorCount": self.tool_error_count,
        }


@dataclass
class HarnessExecutionState:
    """Goal-level state the compact trajectory replays into."""

    run_id: str = field(default_factory=_new_run_id)
    goal_id: str | None = None
    status: ExecutionStatus = "planning"
    steps: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    budgets: ExecutionBudget = field(default_factory=ExecutionBudget)
    last_decision: dict[str, Any] | None = None
    updated_at: str | None = None

    @classmethod
    def new(cls) -> HarnessExecutionState:
        return cls(run_id=_new_run_id())

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> HarnessExecutionState:
        raw = data or {}
        return cls(
            run_id=str(raw.get("runId") or _new_run_id()),
            goal_id=raw.get("goalId"),
            status=raw.get("status") or "planning",
            steps=list(raw.get("steps") or []),
            evidence=list(raw.get("evidence") or []),
            budgets=ExecutionBudget.from_dict(raw.get("budgets")),
            last_decision=raw.get("lastDecision"),
            updated_at=raw.get("updatedAt"),
        )

    def apply_steps(
        self,
        steps: list[dict[str, Any]],
        decision: dict[str, Any] | None,
    ) -> None:
        """Merge latest loop steps and the completion decision."""
        if steps:
            seen = {(s.get("round"), s.get("traceStepId")) for s in self.steps}
            for step in steps:
                key = (step.get("round"), step.get("traceStepId"))
                if key not in seen:
                    self.steps.append(step)
                    seen.add(key)
            self.budgets.step_count = len(self.steps)
            self.budgets.tool_error_count = sum(
                int(s.get("toolErrorCount") or 0) for s in self.steps
            )

        if decision:
            self.last_decision = decision
            status = decision.get("status")
            if status in _STATUS_DRIVING:
                self.status = status

        self.updated_at = _utc_now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "goalId": self.goal_id,
            "status": self.status,
            "steps": self.steps[-50:],
            "evidence": self.evidence[-50:],
            "budgets": self.budgets.to_dict(),
            "lastDecision": self.last_decision,
            "updatedAt": self.updated_at,
        }


TrajectoryEventType = Literal["step_decision"]


@dataclass(frozen=True)
class HarnessTrajectoryEvent:
    """A compact replayable event derived from one loop step."""

    type: TrajectoryEventType
    step: dict[str, Any]
    decision: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "step": self.step, "decision": self.decision}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HarnessTrajectoryEvent | None:
        if not isinstance(data, dict) or data.get("type") != "step_decision":
            return None
        step = data.get("step")
        if not isinstance(step, dict):
            return None
        decision = data.get("decision")
        return cls(
            type="step_decision",
            step=step,
            decision=decision if isinstance(decision, dict) else None,
        )


def build_step_decision_event(
    step: dict[str, Any],
    decision: dict[str, Any] | None,
) -> HarnessTrajectoryEvent:
    return HarnessTrajectoryEvent(type="step_decision", step=step, decision=decision)


def reduce_execution_state(
    initial_state: HarnessExecutionState,
    events: list[HarnessTrajectoryEvent | dict[str, Any]],
) -> HarnessExecutionState:
    """Replay compact trajectory events into a fresh execution state.

    The input state is deep-copied via to_dict/from_dict and never mutated.
    """
    state = HarnessExecutionState.from_dict(initial_state.to_dict())
    for raw_event in events:
        event = (
            HarnessTrajectoryEvent.from_dict(raw_event)
            if isinstance(raw_event, dict)
            else raw_event
        )
        if event is None:
            continue
        if event.type == "step_decision":
            state.apply_steps([event.step], event.decision)
    return state
