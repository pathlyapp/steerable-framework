"""Unit tests for the framework-level multi-agent orchestration primitives.

Covers:
* Topological sorting and structural plan validation.
* Coordinator runner (forced tool choice & text-parsing fallbacks).
* Worker event translation (reframing SSE events).
* Parallel and sequential execution via OrchestrationExecutor with dependency skipping.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Sequence, Iterable
import pytest

from steerable_agent_protocol.generated import SSEEvent, ToolCall
from steerable_agent_runtime import LLMMessage, LLMStreamChunk
from steerable_agent_runtime.orchestration import (
    OrchestrationExecutor,
    OrchestrationPlan,
    OrchestrationTask,
    PlanValidationError,
    run_coordinator,
    run_worker,
    validate_plan,
    topological_layers,
    reframe_worker_event,
    build_peer_outputs_block,
    WorkerResult,
)


# ---------------------------------------------------------------------------
# Test Plan Validation & Topological Sort
# ---------------------------------------------------------------------------

def test_validate_plan_valid():
    raw = {
        "rationale": "Need to fetch data and then analyze it.",
        "mode": "dag",
        "tasks": [
            {
                "id": "t1",
                "agentId": "searcher",
                "prompt": "Find top restaurants in Paris.",
                "dependsOn": [],
                "readOutputsFrom": [],
            },
            {
                "id": "t2",
                "agentId": "summarizer",
                "prompt": "Summarize results.",
                "dependsOn": ["t1"],
                "readOutputsFrom": ["t1"],
            },
        ],
    }
    plan = validate_plan(raw, allowed_agent_ids={"searcher", "summarizer"}, require_full_coverage=True)
    assert plan.rationale == "Need to fetch data and then analyze it."
    assert len(plan.tasks) == 2
    assert plan.tasks[0].id == "t1"
    assert plan.tasks[1].dependsOn == ["t1"]


def test_validate_plan_coercion():
    # Covers double-stringification / nested Anthropic escaping
    raw = {
        "rationale": "Escaped tasks",
        "tasks": '[{"id": "t1", "agentId": "agent-a", "prompt": "p1", "dependsOn": "[]"}]',
    }
    plan = validate_plan(raw, allowed_agent_ids={"agent-a"}, require_full_coverage=True)
    assert len(plan.tasks) == 1
    assert plan.tasks[0].id == "t1"
    assert plan.tasks[0].dependsOn == []


def test_validate_plan_failures():
    # Duplicate ID
    raw = {
        "tasks": [
            {"id": "t1", "agentId": "a1", "prompt": "p1"},
            {"id": "t1", "agentId": "a1", "prompt": "p2"},
        ]
    }
    with pytest.raises(PlanValidationError, match="task id 重复"):
        validate_plan(raw, allowed_agent_ids={"a1"}, require_full_coverage=True)

    # Disallowed Agent
    raw = {
        "tasks": [
            {"id": "t1", "agentId": "evil_agent", "prompt": "p1"},
        ]
    }
    with pytest.raises(PlanValidationError, match="指向了不允许的 agentId"):
        validate_plan(raw, allowed_agent_ids={"a1"}, require_full_coverage=True)

    # Self dependency
    raw = {
        "tasks": [
            {"id": "t1", "agentId": "a1", "prompt": "p1", "dependsOn": ["t1"]},
        ]
    }
    with pytest.raises(PlanValidationError, match="不能依赖自己"):
        validate_plan(raw, allowed_agent_ids={"a1"}, require_full_coverage=True)


def test_topological_layers_simple():
    plan = OrchestrationPlan(
        tasks=[
            OrchestrationTask(id="t1", agentId="a1", prompt="p1"),
            OrchestrationTask(id="t2", agentId="a2", prompt="p2", dependsOn=["t1"]),
            OrchestrationTask(id="t3", agentId="a3", prompt="p3", dependsOn=["t1"]),
            OrchestrationTask(id="t4", agentId="a4", prompt="p4", dependsOn=["t2", "t3"]),
        ]
    )
    layers = topological_layers(plan)
    assert layers == [["t1"], ["t2", "t3"], ["t4"]]


def test_topological_layers_cycle():
    plan = OrchestrationPlan(
        tasks=[
            OrchestrationTask(id="t1", agentId="a1", prompt="p1", dependsOn=["t2"]),
            OrchestrationTask(id="t2", agentId="a2", prompt="p2", dependsOn=["t1"]),
        ]
    )
    with pytest.raises(PlanValidationError, match="存在循环依赖"):
        topological_layers(plan)


# ---------------------------------------------------------------------------
# Test SSE Event Reframing
# ---------------------------------------------------------------------------

def test_reframe_worker_event_content():
    evt = SSEEvent(type="content", payload={"text": "Hello"})
    reframed = reframe_worker_event(evt, group_id="g123", task_id="t456")
    assert reframed is not None
    assert reframed.type == "orchestration"
    assert reframed.event == "task_chunk"
    assert reframed.orchestrationGroupId == "g123"
    assert reframed.taskId == "t456"
    assert reframed.content == "Hello"


def test_reframe_worker_event_agent_dropped():
    evt = SSEEvent(type="agent", event="round_start")
    reframed = reframe_worker_event(evt, group_id="g123", task_id="t456")
    assert reframed is None


def test_reframe_worker_event_agent_preserved_and_stamped():
    evt = SSEEvent(type="agent", event="custom_agent_action")
    reframed = reframe_worker_event(evt, group_id="g123", task_id="t456")
    assert reframed is not None
    assert reframed.orchestrationGroupId == "g123"
    assert reframed.taskId == "t456"


# ---------------------------------------------------------------------------
# Mocks & Helpers for Coordinator / Worker loops
# ---------------------------------------------------------------------------

class MockLLMProvider:
    name = "mock"
    model = "mock-model"

    def __init__(self, responses: list[list[LLMStreamChunk]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages, **kwargs):
        raise NotImplementedError

    async def stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        self.calls.append({"messages": list(messages), "tools": list(tools or []), "kwargs": kwargs})
        resp = self.responses.pop(0) if self.responses else []
        for chunk in resp:
            yield chunk


# ---------------------------------------------------------------------------
# Test Coordinator Loop Runner
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_coordinator_happy_path():
    # Coordinator is forced to make a plan tool call.
    chunks = [
        LLMStreamChunk(
            tool_call_delta=ToolCall(
                id="tc0",
                name="make_orchestration_plan",
                arguments={
                    "rationale": "Parallel research",
                    "mode": "parallel",
                    "tasks": [
                        {"id": "t1", "agentId": "searcher", "prompt": "Search restaurants"}
                    ],
                },
            )
        ),
        LLMStreamChunk(finish_reason="tool_calls"),
    ]

    provider = MockLLMProvider([chunks])
    res = await run_coordinator(
        provider=provider,
        system_prompt="You are a system prompt.",
        user_message="Find a place to eat.",
        allowed_agent_ids={"searcher"},
        require_full_coverage=True,
    )

    assert res.error is None
    assert res.plan_dict is not None
    assert res.plan_dict["rationale"] == "Parallel research"
    assert len(res.plan_dict["tasks"]) == 1
    assert res.plan_dict["tasks"][0]["agentId"] == "searcher"


@pytest.mark.asyncio
async def test_coordinator_text_fallback():
    # If the model emits plain text containing a json code block instead of a tool call
    plain_text = (
        "Here is the plan:\n"
        "```json\n"
        "{\n"
        '  "rationale": "Text fallback works",\n'
        '  "mode": "sequential",\n'
        '  "tasks": [{"id": "t1", "agentId": "a1", "prompt": "p1"}]\n'
        "}\n"
        "```"
    )
    chunks = [
        LLMStreamChunk(content_delta=plain_text),
        LLMStreamChunk(finish_reason="stop"),
    ]

    provider = MockLLMProvider([chunks])
    res = await run_coordinator(
        provider=provider,
        system_prompt="You are a system prompt.",
        user_message="Do work.",
        allowed_agent_ids={"a1"},
        require_full_coverage=True,
    )

    assert res.error is None
    assert res.plan_dict is not None
    assert res.plan_dict["rationale"] == "Text fallback works"
    assert len(res.plan_dict["tasks"]) == 1


# ---------------------------------------------------------------------------
# Test OrchestrationExecutor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_executor_successful_dag():
    plan = OrchestrationPlan(
        rationale="Dag work",
        tasks=[
            OrchestrationTask(id="t1", agentId="a1", prompt="p1"),
            OrchestrationTask(id="t2", agentId="a2", prompt="p2", dependsOn=["t1"]),
        ]
    )

    async def mock_worker(task: OrchestrationTask, queue: asyncio.Queue[SSEEvent]) -> WorkerResult:
        # Simulate worker SSE emitting
        await queue.put(SSEEvent(type="orchestration", event="task_chunk", content=f"Output from {task.id}"))
        return WorkerResult(
            task_id=task.id,
            agent_id=task.agentId,
            output_text=f"Result of {task.id}",
            goal_completed=True,
        )

    executor = OrchestrationExecutor(
        group_id="g999",
        plan=plan,
        worker_runner=mock_worker,
    )

    events: list[SSEEvent] = []
    async for event in executor.run():
        events.append(event)

    # Check state sequence:
    # 1. plan_ready
    # 2. t1 start
    # 3. t1 chunk
    # 4. t1 done
    # 5. t2 start
    # 6. t2 chunk
    # 7. t2 done
    # 8. done (ok)

    assert len(events) >= 5
    assert events[0].event == "plan_ready"
    assert events[0].orchestrationGroupId == "g999"

    starts = [e.taskId for e in events if e.event == "task_start"]
    dones = [e.taskId for e in events if e.event == "task_done"]
    chunks = [e.content for e in events if e.event == "task_chunk"]

    assert starts == ["t1", "t2"]
    assert dones == ["t1", "t2"]
    assert chunks == ["Output from t1", "Output from t2"]

    # Verify aggregate completion status
    done_evt = next(e for e in events if e.event == "done")
    assert done_evt.content == "ok"


@pytest.mark.asyncio
async def test_executor_cascading_failure():
    plan = OrchestrationPlan(
        rationale="Cascading skip",
        tasks=[
            OrchestrationTask(id="t1", agentId="a1", prompt="p1"),
            OrchestrationTask(id="t2", agentId="a2", prompt="p2", dependsOn=["t1"]),
        ]
    )

    async def mock_worker(task: OrchestrationTask, queue: asyncio.Queue[SSEEvent]) -> WorkerResult:
        # t1 fails, t2 should be skipped automatically
        if task.id == "t1":
            return WorkerResult(
                task_id=task.id,
                agent_id=task.agentId,
                output_text="Failure info",
                error="Something exploded",
                goal_completed=False,
            )
        return WorkerResult(
            task_id=task.id,
            agent_id=task.agentId,
            output_text="Success",
            goal_completed=True,
        )

    executor = OrchestrationExecutor(
        group_id="g999",
        plan=plan,
        worker_runner=mock_worker,
    )

    events: list[SSEEvent] = []
    async for event in executor.run():
        events.append(event)

    # Check status sequence:
    # t1 runs and fails.
    # t2 is skipped.
    # done status is 'aborted' or 'partial'

    dones = {e.taskId: e.content for e in events if e.event == "task_done" and e.taskId}
    assert dones["t1"] == "failed"
    assert dones["t2"] == "skipped"

    done_evt = next(e for e in events if e.event == "done")
    assert done_evt.content == "aborted"
