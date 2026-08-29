"""Wave 3: golden-trajectory eval gate.

Each scenario in ``tests/golden/*.json`` drives a real CoreLoop (scripted
provider + tool table + optional approval/sandbox decorators) and pins the
trajectory the loop emits: the per-round ``step_decision`` entries, the
tool outcomes, the terminal completion, and — for scenarios that wire
storage — the durable record's kind sequence. A loop-logic change that
alters any of these breaks the gate deterministically, with no live LLM.

The ``basic_tool_round`` scenario's golden trajectory is derived from the
cross-language replay fixture ``fixtures/replay/basic.json`` — the two
fixture families share one source of truth for what a clean run records.

Regenerate goldens after an *intentional* loop change:

    STEERABLE_GOLDEN_RECORD=1 uv run pytest tests/test_golden.py

Record mode rewrites only the ``golden`` section of each scenario and must
be reviewed like a snapshot update — a regenerated golden that nobody
reviewed is a regression that got blessed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall, ToolResult

from steerable_agent_runtime import (
    ApprovalDecision,
    ApprovalExecutor,
    AutoApprover,
    CoreLoop,
    LoopConfig,
    LoopEvent,
    RouterToolExecutor,
    SandboxedToolExecutor,
    ToolRouter,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk, LLMUsage
from steerable_agent_runtime.storage import InMemoryStorage

GOLDEN_DIR = Path(__file__).parent / "golden"
RECORD = os.environ.get("STEERABLE_GOLDEN_RECORD") == "1"


def _load_scenarios() -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text())
        for path in sorted(GOLDEN_DIR.glob("*.json"))
    ]


SCENARIOS = _load_scenarios()


class _ScriptedProvider:
    """Replays the scenario's per-round LLM responses."""

    name = "golden"
    model = "golden-model"

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self._script = script
        self._round = 0

    async def complete(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def stream(self, messages: Any, **kwargs: Any) -> Any:
        entry = self._script[min(self._round, len(self._script) - 1)]
        self._round += 1

        async def _gen() -> Any:
            for text in entry.get("contentParts") or []:
                yield LLMStreamChunk(content_delta=text)
            content = entry.get("content")
            if content:
                yield LLMStreamChunk(content_delta=content)
            for call in entry.get("toolCalls") or []:
                yield LLMStreamChunk(
                    tool_call_delta=ToolCall(
                        id=str(call["id"]),
                        name=str(call["name"]),
                        arguments=dict(call.get("arguments") or {}),
                    )
                )
            finish = entry.get("finishReason") or (
                "tool_calls" if entry.get("toolCalls") else "stop"
            )
            yield LLMStreamChunk(
                finish_reason=finish,
                usage=LLMUsage(prompt_tokens=5, completion_tokens=1, total_tokens=6),
            )

        return _gen()


class _FakeSandboxBackend:
    """Deterministic backend for sandbox scenarios (no OS dependency)."""

    name = "golden-sandbox"

    @property
    def enforcement(self) -> str:
        return "full"

    def wrap_command(self, command: str) -> str:
        return f"sandbox-exec -p golden sh -c {command!r}"


def _build_tools(specs: list[dict[str, Any]]) -> ToolRouter:
    router = ToolRouter()
    for spec in specs:
        fails = bool(spec.get("fails"))
        payload = spec.get("result") or {}

        async def _handler(
            _fails: bool = fails, _payload: dict[str, Any] = payload, **kwargs: Any
        ) -> ToolResult:
            if _fails:
                return ToolResult(success=False, error="golden tool failure")
            return ToolResult(success=True, data=dict(_payload))

        router.register(
            _handler,
            name=str(spec["name"]),
            mode=spec.get("mode") or "read",
        )
    return router


def _project(events: list[LoopEvent], trajectory: list[Any]) -> dict[str, Any]:
    """The normalized golden projection of a run (no timestamps/tokens)."""
    # The loop emits a completion per round (status "executing"); the
    # terminal one is the LAST.
    completion = [e for e in events if e.kind == "completion"][-1]
    tool_outcomes = []
    for e in events:
        # tool_call_result only: an errored call yields tool_error AND
        # tool_call_result; projecting both would double-count the call.
        if e.kind == "tool_call_result":
            outcome: dict[str, Any] = {
                "name": e.data["name"],
                "success": e.data.get("success", False),
            }
            if e.data.get("error"):
                outcome["error"] = e.data["error"]
            tool_outcomes.append(outcome)
    return {
        "completion": {
            "status": completion.data["status"],
            "reason": completion.data.get("reason", ""),
        },
        "toolOutcomes": tool_outcomes,
        "trajectory": [
            {**entry.step, "decisionStatus": (entry.decision or {}).get("status")}
            for entry in trajectory
        ],
    }


async def _run_scenario(scenario: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    provider = _ScriptedProvider(scenario["providerScript"])
    router = _build_tools(scenario.get("tools") or [])
    executor: Any = RouterToolExecutor(router)
    if scenario.get("sandbox"):
        executor = SandboxedToolExecutor(executor, _FakeSandboxBackend())
    approval_mode = scenario.get("approval")
    if approval_mode == "auto":
        executor = ApprovalExecutor(executor, AutoApprover())
    elif approval_mode == "abort":

        class _Aborter:
            async def approve(self, request: Any) -> ApprovalDecision:
                return ApprovalDecision("abort", "golden abort")

        executor = ApprovalExecutor(executor, _Aborter())
    config_data = scenario.get("config") or {}
    config = LoopConfig(
        max_rounds=int(config_data.get("maxRounds", 32)),
        max_tool_errors=int(config_data.get("maxToolErrors", 3)),
        tool_dedup=bool(config_data.get("toolDedup", True)),
    )
    storage = InMemoryStorage()
    loop = CoreLoop(
        provider,
        executor,
        config,
        history_store=storage,
        record_id="golden-chat",
    )
    events = [e async for e in loop.run([LLMMessage.text_of("user", "go")])]
    projection = _project(events, loop.trajectory)
    entries = await storage.list_history("golden-chat")
    transcript = json.dumps(entries, ensure_ascii=False, default=str)
    contains = scenario.get("transcriptContains") or []
    missing = [needle for needle in contains if needle not in transcript]
    assert not missing, (
        f"{scenario['name']}: transcript missing expected fragments {missing}"
    )
    return projection, [str(e["kind"]) for e in entries]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
@pytest.mark.asyncio
async def test_golden_trajectory(scenario: dict[str, Any]) -> None:
    projection, history_kinds = await _run_scenario(scenario)
    if scenario.get("assertHistory"):
        projection["historyKinds"] = history_kinds

    if RECORD:
        scenario["golden"] = projection
        path = GOLDEN_DIR / f"{scenario['name']}.json"
        path.write_text(json.dumps(scenario, indent=1, ensure_ascii=False) + "\n")
        return

    golden = scenario.get("golden")
    assert golden is not None, f"{scenario['name']}: no golden recorded yet"
    assert projection == golden, (
        f"golden trajectory drifted for {scenario['name']!r}.\n"
        f"expected: {json.dumps(golden, indent=1, ensure_ascii=False)}\n"
        f"actual:   {json.dumps(projection, indent=1, ensure_ascii=False)}\n"
        "If this loop change is intentional, regenerate with "
        "STEERABLE_GOLDEN_RECORD=1 and review the diff like a snapshot."
    )


def test_basic_scenario_shares_the_crosslang_fixture() -> None:
    """The basic scenario's golden trajectory is the cross-language replay
    fixture's event list — one source of truth for a clean run's record."""
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "replay" / "basic.json").read_text()
    )
    scenario = next(s for s in SCENARIOS if s["name"] == "basic_tool_round")
    if RECORD:
        return
    golden_steps = scenario["golden"]["trajectory"]
    fixture_steps = [e["step"] for e in fixture["events"]]
    assert len(golden_steps) == len(fixture_steps)
    for golden_step, fixture_step in zip(golden_steps, fixture_steps):
        for key in (
            "round",
            "traceStepId",
            "finishReason",
            "toolCalls",
            "toolCallCount",
            "toolErrorCount",
            "textLength",
        ):
            assert golden_step[key] == fixture_step[key], (
                f"basic_tool_round drifted from fixtures/replay/basic.json on {key}"
            )
