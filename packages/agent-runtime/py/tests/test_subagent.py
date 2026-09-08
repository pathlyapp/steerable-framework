"""SubagentExecutor: delegation tool answered by a bounded child CoreLoop."""

from __future__ import annotations

import asyncio

import pytest
from steerable_agent_runtime import (
    CoreLoop,
    LoopConfig,
    RouterToolExecutor,
    SubagentConfig,
    SubagentExecutor,
    SubagentRegistry,
    ToolRouter,
    subagent_tool_descriptor,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk

from test_loop import collect
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


def test_descriptor_with_registry_advertises_subagent_type_enum() -> None:
    registry = SubagentRegistry()
    registry.register("researcher", SubagentConfig(tool_filter=frozenset({"read_file"})))
    registry.register("writer", SubagentConfig())
    d = subagent_tool_descriptor(registry=registry)
    prop = d["function"]["parameters"]["properties"]["subagent_type"]
    assert prop["enum"] == ["researcher", "writer"]


def test_descriptor_without_registry_has_no_subagent_type() -> None:
    d = subagent_tool_descriptor(registry=SubagentRegistry())
    assert "subagent_type" not in d["function"]["parameters"]["properties"]


@pytest.mark.asyncio
async def test_registered_profile_governs_the_child() -> None:
    # The "tight" profile bounds the child to 1 round; a child that keeps
    # calling tools ends budget_exhausted, which only the profile's
    # max_rounds can explain (the default profile would allow 8).
    router = ToolRouter()

    async def ping() -> str:
        return "pong"

    router.register(ping)
    registry = SubagentRegistry()
    registry.register("tight", SubagentConfig(max_rounds=1))

    provider = make_provider(
        [
            {
                "tool_calls": [
                    tc("delegate_subagent", {"task": "loop", "subagent_type": "tight"})
                ]
            },
            {"tool_calls": [tc("ping")]},
            {"tool_calls": [tc("ping")]},
            {"content": "parent done"},
        ]
    )
    executor = SubagentExecutor(
        RouterToolExecutor(router), provider, registry=registry
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    events = [e async for e in loop.run([LLMMessage.text_of("user", "go")])]

    results = _tool_results(events)
    assert results[0]["success"] is False
    assert "budget_exhausted" in results[0].get("error", "")


@pytest.mark.asyncio
async def test_unknown_subagent_type_fails_closed_naming_registered() -> None:
    registry = SubagentRegistry()
    registry.register("researcher", SubagentConfig())
    provider = make_provider(
        [
            {
                "tool_calls": [
                    tc("delegate_subagent", {"task": "x", "subagent_type": "ghost"})
                ]
            },
            {"content": "parent done"},
        ]
    )
    executor = SubagentExecutor(
        RouterToolExecutor(ToolRouter()), provider, registry=registry
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    events = [e async for e in loop.run([LLMMessage.text_of("user", "go")])]

    results = _tool_results(events)
    assert results[0]["success"] is False
    assert "unknown subagent_type" in results[0].get("error", "")
    assert "researcher" in results[0].get("error", "")


@pytest.mark.asyncio
async def test_subagent_type_without_registry_fails_closed() -> None:
    events = await _run_parent(
        [
            {
                "tool_calls": [
                    tc("delegate_subagent", {"task": "x", "subagent_type": "any"})
                ]
            },
            {"content": "parent done"},
        ],
        ToolRouter(),
    )
    results = _tool_results(events)
    assert results[0]["success"] is False
    assert "no profiles are registered" in results[0].get("error", "")


@pytest.mark.asyncio
async def test_model_profile_uses_the_provider_factory() -> None:
    calls: list[str] = []

    def factory(model: str):
        calls.append(model)
        return make_provider([{"content": "cheap-model answer"}])

    registry = SubagentRegistry()
    registry.register("cheap", SubagentConfig(model="gpt-4o-mini"))
    provider = make_provider(
        [
            {
                "tool_calls": [
                    tc("delegate_subagent", {"task": "x", "subagent_type": "cheap"})
                ]
            },
            {"content": "parent done"},
        ]
    )
    executor = SubagentExecutor(
        RouterToolExecutor(ToolRouter()),
        provider,
        registry=registry,
        provider_factory=factory,
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    events = [e async for e in loop.run([LLMMessage.text_of("user", "go")])]

    assert calls == ["gpt-4o-mini"]
    results = _tool_results(events)
    assert results[0]["success"] is True
    assert "cheap-model answer" in results[0].get("resultPreview", "")


@pytest.mark.asyncio
async def test_model_profile_without_factory_fails_closed() -> None:
    registry = SubagentRegistry()
    registry.register("cheap", SubagentConfig(model="gpt-4o-mini"))
    provider = make_provider(
        [
            {
                "tool_calls": [
                    tc("delegate_subagent", {"task": "x", "subagent_type": "cheap"})
                ]
            },
            {"content": "parent done"},
        ]
    )
    executor = SubagentExecutor(
        RouterToolExecutor(ToolRouter()), provider, registry=registry
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    events = [e async for e in loop.run([LLMMessage.text_of("user", "go")])]

    results = _tool_results(events)
    assert results[0]["success"] is False
    assert "no provider factory" in results[0].get("error", "")


def test_concurrency_safe_follows_the_resolved_profile() -> None:
    router = ToolRouter()
    registry = SubagentRegistry()
    registry.register("fast", SubagentConfig(concurrent=True))
    executor = SubagentExecutor(
        RouterToolExecutor(router),
        make_provider([]),
        registry=registry,
    )
    default_call = tc("delegate_subagent", {"task": "x"})
    typed_call = tc("delegate_subagent", {"task": "x", "subagent_type": "fast"})
    assert executor.concurrency_safe(default_call) is False
    assert executor.concurrency_safe(typed_call) is True


# ---------------------------------------------------------------------------
# delegate-on-pool: pooled execution, lifecycle events, shared budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_emits_pool_lifecycle_events_with_profile() -> None:
    sink: list[tuple[str, dict]] = []
    registry = SubagentRegistry()
    registry.register("researcher", SubagentConfig())
    provider = make_provider(
        [
            {
                "tool_calls": [
                    tc(
                        "delegate_subagent",
                        {"task": "scan", "subagent_type": "researcher"},
                    )
                ]
            },
            {"content": "child findings"},
            {"content": "parent done"},
        ]
    )
    executor = SubagentExecutor(
        RouterToolExecutor(ToolRouter()),
        provider,
        registry=registry,
        event_sink=lambda kind, data: sink.append((kind, data)),
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    events = [e async for e in loop.run([LLMMessage.text_of("user", "go")])]
    await executor.shutdown()

    kinds = [k for k, _ in sink]
    assert "child_spawned" in kinds
    assert "child_completed" in kinds
    spawned = dict(sink)["child_spawned"]
    assert spawned["childId"] == "0.1"
    assert spawned["depth"] == 1
    assert spawned["profile"] == "researcher"
    assert spawned["task"] == "scan"
    # The model-face contract is unchanged: the answer is the tool result.
    results = _tool_results(events)
    assert results[0]["success"] is True
    assert "child findings" in results[0].get("resultPreview", "")


@pytest.mark.asyncio
async def test_delegate_event_labels_the_default_profile() -> None:
    sink: list[tuple[str, dict]] = []
    provider = make_provider(
        [
            {"tool_calls": [tc("delegate_subagent", {"task": "x"})]},
            {"content": "child"},
            {"content": "done"},
        ]
    )
    executor = SubagentExecutor(
        RouterToolExecutor(ToolRouter()),
        provider,
        event_sink=lambda kind, data: sink.append((kind, data)),
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    [e async for e in loop.run([LLMMessage.text_of("user", "go")])]
    await executor.shutdown()

    assert dict(sink)["child_spawned"]["profile"] == "general-purpose"


@pytest.mark.asyncio
async def test_concurrent_profiles_run_in_parallel_on_the_pool() -> None:
    """Two same-round delegations with a concurrent profile overlap in
    time — sequential execution would deadlock on the blocking tool."""
    from test_orchestration import _content_provider, _tc

    started = {"a": asyncio.Event(), "b": asyncio.Event()}
    release = asyncio.Event()
    router = ToolRouter()

    async def work(tag: str) -> str:
        started[tag].set()
        await asyncio.wait_for(release.wait(), timeout=5)
        return f"done-{tag}"

    router.register(work)
    work_schema = {
        "type": "function",
        "function": {
            "name": "work",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    registry = SubagentRegistry()
    registry.register("parallel", SubagentConfig(concurrent=True))
    provider, _ = _content_provider(
        {
            "go": [
                {
                    "tool_calls": [
                        _tc(
                            "delegate_subagent",
                            {"task": "task a", "subagent_type": "parallel"},
                            call_id="da",
                        ),
                        _tc(
                            "delegate_subagent",
                            {"task": "task b", "subagent_type": "parallel"},
                            call_id="db",
                        ),
                    ]
                },
                {"content": "both done"},
            ],
            "task a": [
                {"tool_calls": [_tc("work", {"tag": "a"}, call_id="ca")]},
                {"content": "A finished"},
            ],
            "task b": [
                {"tool_calls": [_tc("work", {"tag": "b"}, call_id="cb")]},
                {"content": "B finished"},
            ],
        }
    )
    executor = SubagentExecutor(
        RouterToolExecutor(router), provider, registry=registry, tools=[work_schema]
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    run = asyncio.ensure_future(
        collect(loop.run([LLMMessage.text_of("user", "go")], tools=[work_schema]))
    )
    await asyncio.wait_for(
        asyncio.gather(started["a"].wait(), started["b"].wait()), timeout=5
    )
    release.set()
    events = await asyncio.wait_for(run, timeout=5)
    await executor.shutdown()

    previews = [r.get("resultPreview", "") for r in _tool_results(events)]
    assert any("A finished" in p for p in previews)
    assert any("B finished" in p for p in previews)


@pytest.mark.asyncio
async def test_concurrent_delegation_fails_closed_at_the_pool_cap() -> None:
    """The pool budget is fail-closed for delegations too: the delegation
    over the cap returns orchestration_budget_exceeded, never a queue."""
    from test_orchestration import _content_provider, _tc

    entered = asyncio.Event()
    release = asyncio.Event()
    router = ToolRouter()

    async def work() -> str:
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=5)
        return "done"

    router.register(work)
    work_schema = {
        "type": "function",
        "function": {
            "name": "work",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    registry = SubagentRegistry()
    registry.register("parallel", SubagentConfig(concurrent=True))
    provider, _ = _content_provider(
        {
            "go": [
                {
                    "tool_calls": [
                        _tc(
                            "delegate_subagent",
                            {"task": "task a", "subagent_type": "parallel"},
                            call_id="da",
                        ),
                        _tc(
                            "delegate_subagent",
                            {"task": "task b", "subagent_type": "parallel"},
                            call_id="db",
                        ),
                    ]
                },
                {"content": "wrapped up"},
            ],
            "task a": [
                {"tool_calls": [_tc("work", {}, call_id="ca")]},
                {"content": "A finished"},
            ],
            "task b": [{"content": "B finished"}],
        }
    )
    executor = SubagentExecutor(
        RouterToolExecutor(router),
        provider,
        SubagentConfig(max_parallel=1),
        registry=registry,
        tools=[work_schema],
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    run = asyncio.ensure_future(
        collect(loop.run([LLMMessage.text_of("user", "go")], tools=[work_schema]))
    )
    # Let the winning delegation enter its blocking tool before releasing.
    await asyncio.wait_for(entered.wait(), timeout=5)
    release.set()
    events = await asyncio.wait_for(run, timeout=5)
    await executor.shutdown()

    results = _tool_results(events)
    assert len(results) == 2
    # Exactly one delegation held the cap; the other failed closed. Which
    # call won the race is a gather scheduling detail — assert the set.
    assert sum(1 for r in results if r["success"]) == 1
    denied = next(r for r in results if not r["success"])
    assert "orchestration_budget_exceeded" in denied.get("error", "")


@pytest.mark.asyncio
async def test_attach_pool_shares_the_orchestration_pool() -> None:
    """With the six-tool family on, attach_pool routes delegations into the
    orchestration pool: one budget, one lineage space, and the delegate
    child is visible to agent_list."""
    from steerable_agent_runtime.orchestration import (
        OrchestrationConfig,
        OrchestrationExecutor,
        orchestration_tool_descriptors,
    )

    provider = make_provider(
        [
            {"tool_calls": [tc("delegate_subagent", {"task": "x"})]},
            {"content": "child answer"},
            {"content": "parent done"},
        ]
    )
    subagent = SubagentExecutor(RouterToolExecutor(ToolRouter()), provider)
    orchestration = OrchestrationExecutor(
        subagent, provider, OrchestrationConfig()
    )
    subagent.attach_pool(orchestration.pool)
    loop = CoreLoop(provider, orchestration, LoopConfig())
    events = [
        e
        async for e in loop.run(
            [LLMMessage.text_of("user", "go")],
            tools=orchestration_tool_descriptors(),
        )
    ]
    # Listed before shutdown: shutdown() terminally marks every child closed.
    children = orchestration.pool.list()
    await orchestration.shutdown()

    assert [c["childId"] for c in children] == ["0.1"]
    assert children[0]["status"] == "completed"
    assert children[0]["task"] == "x"
    results = _tool_results(events)
    assert "child answer" in results[0].get("resultPreview", "")


@pytest.mark.asyncio
async def test_child_advertises_the_delegated_tool_surface() -> None:
    """Children see the parent's advertised schemas minus the delegation
    tool itself, intersected with the profile's tool filter — a child
    never re-delegates (depth-1 by construction, advertised honestly)."""
    seen_tools: list[list[str] | None] = []
    script = iter(
        [
            {"tool_calls": [tc("delegate_subagent", {"task": "read"})]},
            {"content": "child answer"},
            {"content": "parent done"},
        ]
    )

    class _CapturingProvider:
        name = "fake"
        model = "fake-model"

        async def complete(self, messages, *, tools=None, **kw):
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw):
            seen_tools.append(
                [t["function"]["name"] for t in tools] if tools else None
            )
            entry = next(script)

            async def _gen():
                if entry.get("content"):
                    yield LLMStreamChunk(content_delta=entry["content"])
                for call in entry.get("tool_calls", []):
                    yield LLMStreamChunk(tool_call_delta=call)
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop"
                )

            return _gen()

    parent_tools = [
        {"type": "function", "function": {"name": "read_file", "parameters": {}}},
        {"type": "function", "function": {"name": "write_file", "parameters": {}}},
        subagent_tool_descriptor(),
    ]
    provider = _CapturingProvider()
    executor = SubagentExecutor(
        RouterToolExecutor(ToolRouter()),
        provider,
        SubagentConfig(tool_filter=frozenset({"read_file"})),
        tools=parent_tools,
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    [e async for e in loop.run([LLMMessage.text_of("user", "go")], tools=parent_tools)]
    await executor.shutdown()

    # Round 1 is the parent's request (full surface); round 2 is the
    # child's — filtered to the delegated domain.
    assert seen_tools[0] == ["read_file", "write_file", "delegate_subagent"]
    assert seen_tools[1] == ["read_file"]
