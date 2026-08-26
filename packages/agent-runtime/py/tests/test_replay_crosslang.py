"""Cross-language replay contract: fixtures in fixtures/replay/ are the shared
reference. This test pins the Python reducer against them; the agent's
`tests/harness/replay-crosslang.test.ts` pins the TypeScript reducer against
the same files. A failure on either side means the two reducers diverged —
fix the diverging side, never regenerate fixtures to silence a diff.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from steerable_agent_runtime import HarnessExecutionState, reduce_execution_state

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "replay"


def load_fixtures() -> list[dict]:
    fixtures = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        fixtures.append(json.loads(path.read_text()))
    return fixtures


FIXTURES = load_fixtures()


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f["name"] for f in FIXTURES])
def test_replay_matches_shared_fixture(fixture: dict) -> None:
    state = HarnessExecutionState.from_dict(fixture["initialState"])
    events = fixture["events"]

    for i, expected in enumerate(fixture["expectedPerEvent"]):
        state = reduce_execution_state(state, events[i : i + 1])
        actual = {
            "status": state.status,
            "stepCount": state.budgets.step_count,
            "toolErrorCount": state.budgets.tool_error_count,
        }
        assert actual == expected, f"event {i} ({fixture['name']}): {actual} != {expected}"

    final = state.to_dict()
    final.pop("updatedAt", None)
    assert final == fixture["expectedFinal"]
