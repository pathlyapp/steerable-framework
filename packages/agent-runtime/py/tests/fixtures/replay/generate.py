"""Generate cross-language replay fixtures (events + expected reduced states).

The fixtures in this directory are the shared replay contract between the
framework's Python reducer and deeppath-agent's TypeScript reducer. Both test
suites load the same JSON files and assert identical per-event snapshots and
identical final states.

Regenerate after any intentional reducer change:

    python packages/agent-runtime/py/tests/fixtures/replay/generate.py

The Python reducer is the reference implementation (ported from deeppath-api),
so expected values are computed with it. If the TypeScript side disagrees on a
fixture, that is a bug to fix — not a reason to regenerate.

Fixture schema:
    name            str
    initialState    HarnessExecutionState dict (fixed runId, no updatedAt)
    events          list of trajectory event dicts (may include unknown types)
    expectedPerEvent  after each event: {status, stepCount, toolErrorCount}
    expectedFinal   full to_dict() of the reduced state, minus updatedAt
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from steerable_agent_runtime import HarnessExecutionState, reduce_execution_state

HERE = Path(__file__).parent


def step(round_: int, **overrides):
    base = {
        "round": round_,
        "traceStepId": f"round_{round_}",
        "finishReason": "tool_calls",
        "toolCalls": ["create_task"],
        "toolCallCount": 1,
        "toolErrorCount": 0,
        "textLength": 0,
    }
    base.update(overrides)
    return base


def decision(status: str, **overrides):
    base = {"status": status, "reason": "r", "confidence": 0.8, "evidence": []}
    base.update(overrides)
    return base


def ev(step_dict, decision_dict):
    return {"type": "step_decision", "step": step_dict, "decision": decision_dict}


def initial_state() -> dict:
    return {
        "runId": "hrun_fixture",
        "goalId": "goal_fixture",
        "status": "planning",
        "steps": [],
        "evidence": [],
        "budgets": {"maxSteps": 10, "maxToolErrors": 2, "stepCount": 0, "toolErrorCount": 0},
        "lastDecision": None,
        "updatedAt": None,
    }


def snapshot(state: HarnessExecutionState) -> dict:
    return {
        "status": state.status,
        "stepCount": state.budgets.step_count,
        "toolErrorCount": state.budgets.tool_error_count,
    }


def build_fixture(name: str, events: list[dict]) -> dict:
    state = HarnessExecutionState.from_dict(initial_state())
    per_event = []
    for i in range(len(events)):
        state = reduce_execution_state(state, events[i : i + 1])
        per_event.append(snapshot(state))
    final = state.to_dict()
    final.pop("updatedAt", None)
    return {
        "name": name,
        "initialState": initial_state(),
        "events": events,
        "expectedPerEvent": per_event,
        "expectedFinal": final,
    }


def fixture_basic() -> dict:
    events = [
        ev(step(0), decision("executing")),
        ev(step(1), decision("executing")),
        ev(step(2, finishReason="stop", toolCalls=[], toolCallCount=0, textLength=42),
           decision("completed", reason="done", confidence=0.9,
                    evidence=[{"kind": "text", "ref": "final"}])),
    ]
    return build_fixture("basic", events)


def fixture_dedupe_and_unknown() -> dict:
    string_round = step(2)
    string_round["round"] = "2"  # type drift on the wire — same logical step
    events = [
        ev(step(0), decision("executing")),
        ev(step(0), decision("executing")),  # exact duplicate — deduped
        {"type": "bogus_event", "step": step(1)},  # unknown type — skipped
        ev(step(1), decision("planning")),  # non-driving status — status stays
        ev(step(1), decision("verifying")),  # same dedupe key — skipped entirely
        ev(step(2), decision("waiting_user", reason="need_input")),
        # String "2" vs int 2: the dedupe key string-coerces on both sides,
        # so this is the same step and must be deduped.
        ev(string_round, decision("executing")),
        ev(step(3), decision("executing")),
    ]
    return build_fixture("dedupe_and_unknown", events)


def fixture_budget_exhausted() -> dict:
    events = [
        ev(step(0, toolErrorCount=1), decision("executing")),
        ev(step(1, toolErrorCount=1), decision("executing")),
        ev(step(2, toolErrorCount=1), decision("budget_exhausted", reason="tool_errors")),
    ]
    return build_fixture("budget_exhausted", events)


def fixture_fuzz() -> dict:
    rng = random.Random(20260825)
    statuses = ["executing", "executing", "executing", "planning", "verifying",
                "waiting_user", "waiting_approval"]
    events: list[dict] = []
    for i in range(120):
        if i and rng.random() < 0.15:
            # Re-emit an earlier step (transport retry) — must dedupe.
            earlier = rng.randrange(i)
            events.append(ev(step(earlier), decision(rng.choice(statuses))))
            continue
        if rng.random() < 0.05:
            events.append({"type": f"unknown_{i}", "step": step(i)})
            continue
        terminal = i >= 118
        status = "completed" if terminal else rng.choice(statuses)
        events.append(ev(
            step(i, toolErrorCount=rng.randrange(3) if rng.random() < 0.2 else 0),
            decision(status),
        ))
    return build_fixture("fuzz", events)


FIXTURES = [fixture_basic, fixture_dedupe_and_unknown, fixture_budget_exhausted, fixture_fuzz]


def main() -> None:
    for factory in FIXTURES:
        fixture = factory()
        path = HERE / f"{fixture['name']}.json"
        path.write_text(json.dumps(fixture, indent=1, ensure_ascii=False) + "\n")
        print(f"[generate] {path.name}: {len(fixture['events'])} events")


if __name__ == "__main__":
    main()
