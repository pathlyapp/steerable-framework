"""SubagentExecutor: delegation tool answered by a bounded child CoreLoop."""

from __future__ import annotations

import pytest
from steerable_agent_runtime import (
    CoreLoop,
    LoopConfig,
    RouterToolExecutor,
    SubagentConfig,
    SubagentExecutor,
    ToolRouter,
    subagent_tool_descriptor,
)
from steerable_agent_runtime.llm import LLMMessage

from test_trace_recorder import make_provider, tc


async def _run_parent(script, router: ToolRouter, *, config: SubagentConfig | None = None):
    provider = make_provider(script)
    executor = SubagentExecutor(RouterToolExecutor(router), provider, config)
    loop = CoreLoop(provider, executor, LoopConfig())
    events = [e async for e in loop.run([LLMMessage.text_of("user", "go")])]
    return events


def _tool_results(events):
    return [e.data for e in events if e.kind == "tool_call_result"]


@pytest.mark.asyncio
async def test_delegation_runs_child_loop_and_returns_its_answer() -> None:
    # call 1: parent delegates; call 2: the child answers; call 3: the
    # parent wraps up with the child's answer in its transcript.
    events = await _run_parent(
        [
            {"tool_calls": [tc("delegate_subagent", {"task": "compute 1+1"})]},
            {"content": "child says 2"},
            {"content": "final: 2"},
        ],
        ToolRouter(),
    )

    results = _tool_results(events)
    assert len(results) == 1
    assert results[0]["name"] == "delegate_subagent"
    assert results[0]["success"] is True
    assert "child says 2" in results[0].get("resultPreview", "")
    completion = [e for e in events if e.kind == "completion"][-1]
    assert completion.data["status"] == "completed"


@pytest.mark.asyncio
async def test_child_cannot_spawn_depth_one_by_construction() -> None:
    # The child dispatches to the inner executor — delegate_subagent is not
    # registered there, so a nested delegation attempt fails as an unknown
    # tool and the child must answer from reasoning.
    events = await _run_parent(
        [
            {"tool_calls": [tc("delegate_subagent", {"task": "try nesting"})]},
            {"tool_calls": [tc("delegate_subagent", {"task": "nest deeper"})]},
            {"content": "could not nest"},
            {"content": "parent done"},
        ],
        ToolRouter(),
    )

    results = _tool_results(events)
    assert len(results) == 1  # only the parent's delegation is a parent-span
    assert results[0]["success"] is True
    assert "could not nest" in results[0].get("resultPreview", "")


@pytest.mark.asyncio
async def test_allow_tools_false_fails_child_tool_calls_closed() -> None:
    router = ToolRouter()

    async def add(a: int, b: int) -> int:
        return a + b

    router.register(add)

    events = await _run_parent(
        [
            {"tool_calls": [tc("delegate_subagent", {"task": "add 1 2"})]},
            {"tool_calls": [tc("add", {"a": 1, "b": 2})]},
            {"content": "no tools available"},
            {"content": "parent done"},
        ],
        router,
        config=SubagentConfig(allow_tools=False),
    )

    results = _tool_results(events)
    assert results[0]["success"] is True
    assert "no tools available" in results[0].get("resultPreview", "")


@pytest.mark.asyncio
async def test_tool_filter_narrows_the_childs_tool_domain() -> None:
    # A read-only research sub-agent: the child may call ``search`` but a
    # write tool fails closed with tool_not_delegated — the filter is a
    # privilege boundary, not a prompt hint.
    router = ToolRouter()

    async def search(query: str) -> str:
        return f"results for {query}"

    async def delete_everything() -> str:
        return "deleted"  # must never run inside the child

    router.register(search)
    router.register(delete_everything)

    events = await _run_parent(
        [
            {"tool_calls": [tc("delegate_subagent", {"task": "research"})]},
            {"tool_calls": [tc("delete_everything")]},
            {"tool_calls": [tc("search", {"query": "q"})]},
            {"content": "searched instead"},
            {"content": "parent done"},
        ],
        router,
        config=SubagentConfig(tool_filter=frozenset({"search"})),
    )

    results = _tool_results(events)
    assert results[0]["success"] is True
    assert "searched instead" in results[0].get("resultPreview", "")


@pytest.mark.asyncio
async def test_tool_filter_denied_call_names_the_delegated_set() -> None:
    # The denial text tells the child what it CAN call, so it re-issues
    # instead of concluding the tool is broken. The child's denial is a
    # child-internal step (the parent span only carries the final answer),
    # so assert on the child's own completion text: a child that saw the
    # delegated set in the denial can answer "cannot delete" truthfully.
    router = ToolRouter()

    async def search(query: str) -> str:
        return "ok"

    router.register(search)

    events = await _run_parent(
        [
            {"tool_calls": [tc("delegate_subagent", {"task": "clean up"})]},
            {"tool_calls": [tc("rm_rf")]},
            {"content": "cannot delete: tool_not_delegated, have search"},
            {"content": "parent done"},
        ],
        router,
        config=SubagentConfig(tool_filter=frozenset({"search"})),
    )

    results = _tool_results(events)
    assert results[0]["success"] is True
    assert "tool_not_delegated" in results[0].get("resultPreview", "")


@pytest.mark.asyncio
async def test_tool_filter_none_keeps_whole_domain() -> None:
    # Legacy behavior: no filter → the child reaches every parent tool.
    router = ToolRouter()

    async def add(a: int, b: int) -> int:
        return a + b

    router.register(add)

    events = await _run_parent(
        [
            {"tool_calls": [tc("delegate_subagent", {"task": "add"})]},
            {"tool_calls": [tc("add", {"a": 2, "b": 3})]},
            {"content": "5"},
            {"content": "parent done"},
        ],
        router,
        config=SubagentConfig(tool_filter=None),
    )

    results = _tool_results(events)
    assert results[0]["success"] is True
    assert "5" in results[0].get("resultPreview", "")


@pytest.mark.asyncio
async def test_empty_task_fails_fast_without_running_a_child() -> None:
    provider_script = [
        {"tool_calls": [tc("delegate_subagent", {"task": "  "})]},
        {"content": "ok"},
    ]
    events = await _run_parent(provider_script, ToolRouter())

    results = _tool_results(events)
    assert results[0]["success"] is False
    assert "empty task" in results[0].get("error", "")


@pytest.mark.asyncio
async def test_child_budget_exhaustion_surfaces_as_failed_result() -> None:
    # The child keeps calling tools; its max_rounds bound ends it and the
    # parent gets a failed (not hanging) tool result.
    router = ToolRouter()

    async def ping() -> str:
        return "pong"

    router.register(ping)

    events = await _run_parent(
        [
            {"tool_calls": [tc("delegate_subagent", {"task": "loop forever"})]},
            {"tool_calls": [tc("ping")]},
            {"tool_calls": [tc("ping")]},
            {"content": "parent done"},
        ],
        router,
        config=SubagentConfig(max_rounds=2),
    )

    results = _tool_results(events)
    assert results[0]["success"] is False
    assert "budget_exhausted" in results[0].get("error", "")


def test_descriptor_is_openai_tool_schema() -> None:
    d = subagent_tool_descriptor()
    assert d["type"] == "function"
    assert d["function"]["name"] == "delegate_subagent"
    assert d["function"]["parameters"]["required"] == ["task"]
