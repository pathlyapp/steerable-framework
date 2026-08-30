"""AgentPool + OrchestrationExecutor: parent loop orchestrating parallel
child CoreLoops (P3.1).

The scripted ``make_provider`` serves entries in global call order, which
parallel children make nondeterministic — so these tests route by content:
each request is matched on its transcript's first user message.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_runtime import (
    CoreLoop,
    LoopConfig,
    RouterToolExecutor,
    ToolRouter,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk
from steerable_agent_runtime.orchestration import (
    OrchestrationConfig,
    OrchestrationExecutor,
    orchestration_tool_descriptors,
)
from test_loop import collect, final_completion


def _content_provider(routes: dict[str, list[dict[str, Any]]]):
    """Scripted provider keyed on the first user message of each transcript.

    Each route serves its entries in order (last entry repeats). ``seen``
    captures every request's transcript for steer assertions.
    """
    counters = {key: 0 for key in routes}
    seen: dict[str, list[list[LLMMessage]]] = {key: [] for key in routes}

    class _Provider:
        name = "fake"
        model = "fake-model"

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(
            self, messages, *, tools=None, **kw
        ) -> AsyncIterator[LLMStreamChunk]:
            key = next(
                (
                    k
                    for k in routes
                    if any(
                        m.role == "user" and k in m.content_text for m in messages[:1]
                    )
                ),
                None,
            )
            assert key is not None, (
                f"no route for transcript: {messages[0].content_text!r}"
            )
            seen[key].append(list(messages))
            idx = counters[key]
            counters[key] += 1
            entry = routes[key][min(idx, len(routes[key]) - 1)]

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                for call in entry.get("tool_calls") or []:
                    yield LLMStreamChunk(tool_call_delta=call)
                if entry.get("content"):
                    yield LLMStreamChunk(content_delta=entry["content"])
                yield LLMStreamChunk(finish_reason="stop")

            return _gen()

    return _Provider(), seen


def _tc(name: str, args: dict[str, Any] | None = None, *, call_id: str | None = None):
    from steerable_agent_protocol.generated import ToolCall

    return ToolCall(
        id=call_id or f"call_{name}_{len(name)}", name=name, arguments=args or {}
    )


def _tool_results(events) -> list[dict[str, Any]]:
    return [e.data for e in events if e.kind == "tool_call_result"]


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    """Unwrap the ToolResult envelope: preview → envelope → inner message."""
    envelope = json.loads(result["resultPreview"])
    return json.loads(envelope["message"])


def _error(result: dict[str, Any]) -> str:
    return json.loads(result["resultPreview"]).get("error") or ""


def _orch_tools() -> list[dict[str, Any]]:
    return orchestration_tool_descriptors()


# ---------------------------------------------------------------------------
# spawn + wait
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_and_wait_returns_child_answer() -> None:
    provider, _ = _content_provider(
        {
            "go": [
                {
                    "tool_calls": [
                        _tc("agent_spawn", {"task": "child task"}, call_id="s1")
                    ]
                },
                {"tool_calls": [_tc("agent_wait", {"childId": "0.1"}, call_id="w1")]},
                {"content": "parent done"},
            ],
            "child task": [{"content": "child answer"}],
        }
    )
    executor = OrchestrationExecutor(
        RouterToolExecutor(ToolRouter()), provider, OrchestrationConfig()
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    events = await collect(
        loop.run([LLMMessage.text_of("user", "go")], tools=_orch_tools())
    )
    await executor.shutdown()

    assert final_completion(events)["status"] == "completed"
    results = _tool_results(events)
    spawn, wait = results
    assert spawn["name"] == "agent_spawn" and spawn["success"] is True
    assert _payload(spawn)["childId"] == "0.1"
    assert wait["name"] == "agent_wait" and wait["success"] is True
    payload = _payload(wait)
    assert payload["status"] == "completed"
    assert "child answer" in payload["answer"]


@pytest.mark.asyncio
async def test_two_children_run_in_parallel() -> None:
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

    provider, _ = _content_provider(
        {
            "go": [
                {
                    "tool_calls": [
                        _tc("agent_spawn", {"task": "task a"}, call_id="sa"),
                        _tc("agent_spawn", {"task": "task b"}, call_id="sb"),
                    ]
                },
                {
                    "tool_calls": [
                        _tc("agent_wait", {"childId": "0.1"}, call_id="wa"),
                        _tc("agent_wait", {"childId": "0.2"}, call_id="wb"),
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
    executor = OrchestrationExecutor(
        RouterToolExecutor(router),
        provider,
        OrchestrationConfig(),
        tools=[*_orch_tools(), work_schema],
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    run = asyncio.ensure_future(
        collect(
            loop.run(
                [LLMMessage.text_of("user", "go")], tools=[*_orch_tools(), work_schema]
            )
        )
    )

    # Both children must enter their blocking tool before either is
    # released — sequential execution would deadlock here and time out.
    await asyncio.wait_for(
        asyncio.gather(started["a"].wait(), started["b"].wait()), timeout=5
    )
    release.set()
    events = await asyncio.wait_for(run, timeout=5)
    await executor.shutdown()

    assert final_completion(events)["status"] == "completed"
    waits = [r for r in _tool_results(events) if r["name"] == "agent_wait"]
    answers = {_payload(r)["answer"] for r in waits}
    assert answers == {"A finished", "B finished"}


# ---------------------------------------------------------------------------
# budgets fail closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_cap_fails_closed_not_queued() -> None:
    release = asyncio.Event()
    router = ToolRouter()

    async def work(tag: str) -> str:
        await asyncio.wait_for(release.wait(), timeout=5)
        return f"done-{tag}"

    router.register(work)
    provider, _ = _content_provider(
        {
            "go": [
                {
                    "tool_calls": [
                        _tc("agent_spawn", {"task": "task a"}, call_id="sa"),
                        _tc("agent_spawn", {"task": "task b"}, call_id="sb"),
                    ]
                },
                {"tool_calls": [_tc("agent_close", {"childId": "0.1"}, call_id="c1")]},
                {"content": "done"},
            ],
            "task a": [
                {"tool_calls": [_tc("work", {"tag": "a"}, call_id="ca")]},
                {"content": "A"},
            ],
            "task b": [{"content": "B"}],
        }
    )
    executor = OrchestrationExecutor(
        RouterToolExecutor(router),
        provider,
        OrchestrationConfig(max_parallel=1),
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    run = asyncio.ensure_future(
        collect(loop.run([LLMMessage.text_of("user", "go")], tools=_orch_tools()))
    )
    await asyncio.sleep(0.1)  # let the first child start
    release.set()
    events = await asyncio.wait_for(run, timeout=5)
    await executor.shutdown()

    spawns = [r for r in _tool_results(events) if r["name"] == "agent_spawn"]
    assert spawns[0]["success"] is True
    assert spawns[1]["success"] is False
    assert "orchestration_budget_exceeded" in _error(spawns[1])


@pytest.mark.asyncio
async def test_depth_cap_by_construction() -> None:
    # max_depth=1: the child has no orchestration tools — a spawn attempt
    # fails as an unknown tool rather than reaching a pool.
    provider, _ = _content_provider(
        {
            "go": [
                {
                    "tool_calls": [
                        _tc("agent_spawn", {"task": "child task"}, call_id="s1")
                    ]
                },
                {"tool_calls": [_tc("agent_wait", {"childId": "0.1"}, call_id="w1")]},
                {"content": "done"},
            ],
            "child task": [
                {
                    "tool_calls": [
                        _tc("agent_spawn", {"task": "grandchild"}, call_id="gs")
                    ]
                },
                {"content": "cannot spawn"},
            ],
        }
    )
    executor = OrchestrationExecutor(
        RouterToolExecutor(ToolRouter()), provider, OrchestrationConfig(max_depth=1)
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    events = await collect(
        loop.run([LLMMessage.text_of("user", "go")], tools=_orch_tools())
    )
    await executor.shutdown()

    wait = next(r for r in _tool_results(events) if r["name"] == "agent_wait")
    assert "cannot spawn" in _payload(wait)["answer"]


@pytest.mark.asyncio
async def test_depth_two_allows_a_grandchild() -> None:
    provider, _ = _content_provider(
        {
            "go": [
                {
                    "tool_calls": [
                        _tc("agent_spawn", {"task": "child task"}, call_id="s1")
                    ]
                },
                {"tool_calls": [_tc("agent_wait", {"childId": "0.1"}, call_id="w1")]},
                {"content": "done"},
            ],
            "child task": [
                {
                    "tool_calls": [
                        _tc("agent_spawn", {"task": "grandchild task"}, call_id="gs")
                    ]
                },
                {"tool_calls": [_tc("agent_wait", {"childId": "0.1.1"}, call_id="gw")]},
                {"content": "child got: gc answer"},
            ],
            "grandchild task": [{"content": "gc answer"}],
        }
    )
    executor = OrchestrationExecutor(
        RouterToolExecutor(ToolRouter()), provider, OrchestrationConfig(max_depth=2)
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    events = await collect(
        loop.run([LLMMessage.text_of("user", "go")], tools=_orch_tools())
    )
    await executor.shutdown()

    wait = next(r for r in _tool_results(events) if r["name"] == "agent_wait")
    assert "child got: gc answer" in _payload(wait)["answer"]


# ---------------------------------------------------------------------------
# coordination primitives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_steers_running_child() -> None:
    release = asyncio.Event()
    router = ToolRouter()

    async def work(tag: str) -> str:
        await asyncio.wait_for(release.wait(), timeout=5)
        return "ok"

    router.register(work)
    provider, seen = _content_provider(
        {
            "go": [
                {
                    "tool_calls": [
                        _tc("agent_spawn", {"task": "child task"}, call_id="s1")
                    ]
                },
                {
                    "tool_calls": [
                        _tc(
                            "agent_send",
                            {"childId": "0.1", "message": "extra context"},
                            call_id="m1",
                        )
                    ]
                },
                {"tool_calls": [_tc("agent_wait", {"childId": "0.1"}, call_id="w1")]},
                {"content": "done"},
            ],
            "child task": [
                {"tool_calls": [_tc("work", {"tag": "a"}, call_id="ca")]},
                {"content": "child done"},
            ],
        }
    )
    executor = OrchestrationExecutor(
        RouterToolExecutor(router), provider, OrchestrationConfig()
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    run = asyncio.ensure_future(
        collect(loop.run([LLMMessage.text_of("user", "go")], tools=_orch_tools()))
    )
    await asyncio.sleep(0.2)
    release.set()
    events = await asyncio.wait_for(run, timeout=5)
    await executor.shutdown()

    assert final_completion(events)["status"] == "completed"
    # The child's second-round request carries the steered user message.
    child_requests = seen["child task"]
    assert len(child_requests) >= 2
    second_round = child_requests[1]
    assert any(
        m.role == "user" and "extra context" in m.content_text for m in second_round
    )


@pytest.mark.asyncio
async def test_close_cancels_child_cooperatively() -> None:
    never = asyncio.Event()
    router = ToolRouter()

    async def work(tag: str) -> str:
        await asyncio.wait_for(never.wait(), timeout=10)
        return "unreachable"

    router.register(work)
    provider, _ = _content_provider(
        {
            "go": [
                {
                    "tool_calls": [
                        _tc("agent_spawn", {"task": "child task"}, call_id="s1")
                    ]
                },
                {"tool_calls": [_tc("agent_close", {"childId": "0.1"}, call_id="c1")]},
                {"tool_calls": [_tc("agent_wait", {"childId": "0.1"}, call_id="w1")]},
                {"content": "done"},
            ],
            "child task": [
                {"tool_calls": [_tc("work", {"tag": "a"}, call_id="ca")]},
                {"content": "unreachable"},
            ],
        }
    )
    executor = OrchestrationExecutor(
        RouterToolExecutor(router), provider, OrchestrationConfig()
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    events = await collect(
        loop.run([LLMMessage.text_of("user", "go")], tools=_orch_tools())
    )
    await executor.shutdown()

    wait = next(r for r in _tool_results(events) if r["name"] == "agent_wait")
    assert _payload(wait)["status"] == "cancelled"


@pytest.mark.asyncio
async def test_wait_timeout_reports_running_then_completes() -> None:
    release = asyncio.Event()
    router = ToolRouter()

    async def work(tag: str) -> str:
        await asyncio.wait_for(release.wait(), timeout=5)
        return "ok"

    router.register(work)
    provider, _ = _content_provider(
        {
            "go": [
                {
                    "tool_calls": [
                        _tc("agent_spawn", {"task": "child task"}, call_id="s1")
                    ]
                },
                {
                    "tool_calls": [
                        _tc(
                            "agent_wait",
                            {"childId": "0.1", "timeoutMs": 50},
                            call_id="w1",
                        )
                    ]
                },
                {"tool_calls": [_tc("agent_wait", {"childId": "0.1"}, call_id="w2")]},
                {"content": "done"},
            ],
            "child task": [
                {"tool_calls": [_tc("work", {"tag": "a"}, call_id="ca")]},
                {"content": "child done"},
            ],
        }
    )
    executor = OrchestrationExecutor(
        RouterToolExecutor(router), provider, OrchestrationConfig()
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    run = asyncio.ensure_future(
        collect(loop.run([LLMMessage.text_of("user", "go")], tools=_orch_tools()))
    )
    await asyncio.sleep(0.3)  # first wait times out while the child is blocked
    release.set()
    events = await asyncio.wait_for(run, timeout=5)
    await executor.shutdown()

    waits = [r for r in _tool_results(events) if r["name"] == "agent_wait"]
    first, second = waits
    assert first["success"] is True
    assert _payload(first)["status"] == "running"
    assert _payload(second)["status"] == "completed"


@pytest.mark.asyncio
async def test_unknown_child_id_fails_closed() -> None:
    provider, _ = _content_provider(
        {
            "go": [
                {"tool_calls": [_tc("agent_wait", {"childId": "9.9"}, call_id="w1")]},
                {"content": "done"},
            ],
        }
    )
    executor = OrchestrationExecutor(
        RouterToolExecutor(ToolRouter()), provider, OrchestrationConfig()
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    events = await collect(
        loop.run([LLMMessage.text_of("user", "go")], tools=_orch_tools())
    )
    await executor.shutdown()

    wait = _tool_results(events)[0]
    assert wait["success"] is False
    assert "unknown_child" in _error(wait)


# ---------------------------------------------------------------------------
# lineage events + record reconstruction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_sink_receives_lineage_events() -> None:
    sink: list[tuple[str, dict[str, Any]]] = []
    provider, _ = _content_provider(
        {
            "go": [
                {
                    "tool_calls": [
                        _tc("agent_spawn", {"task": "child task"}, call_id="s1")
                    ]
                },
                {"tool_calls": [_tc("agent_wait", {"childId": "0.1"}, call_id="w1")]},
                {"content": "done"},
            ],
            "child task": [{"content": "child answer"}],
        }
    )
    executor = OrchestrationExecutor(
        RouterToolExecutor(ToolRouter()),
        provider,
        OrchestrationConfig(),
        event_sink=lambda kind, data: sink.append((kind, data)),
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    await collect(loop.run([LLMMessage.text_of("user", "go")], tools=_orch_tools()))
    await executor.shutdown()

    kinds = [k for k, _ in sink]
    assert "child_spawned" in kinds
    assert "child_completed" in kinds
    spawned = dict(sink)["child_spawned"]
    assert spawned["childId"] == "0.1"
    assert spawned["depth"] == 1


@pytest.mark.asyncio
async def test_parent_record_reconstructs_the_delegation() -> None:
    """The parent record alone rebuilds who was spawned and how they ended —
    no dangling tool calls, outcomes as structured result text."""
    provider, _ = _content_provider(
        {
            "go": [
                {
                    "tool_calls": [
                        _tc("agent_spawn", {"task": "child task"}, call_id="s1")
                    ]
                },
                {"tool_calls": [_tc("agent_wait", {"childId": "0.1"}, call_id="w1")]},
                {"content": "done"},
            ],
            "child task": [{"content": "child answer"}],
        }
    )
    executor = OrchestrationExecutor(
        RouterToolExecutor(ToolRouter()), provider, OrchestrationConfig()
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    await collect(loop.run([LLMMessage.text_of("user", "go")], tools=_orch_tools()))
    await executor.shutdown()

    record = loop.history.record
    issued = {tc.id for item in record for tc in (item.message.tool_calls or [])}
    answered = {
        item.message.tool_call_id for item in record if item.message.tool_call_id
    }
    assert issued == answered, "dangling tool calls in the parent record"
    texts = [item.message.content_text for item in record]
    assert any('"childId": "0.1"' in t for t in texts)
    assert any('"status": "completed"' in t and "child answer" in t for t in texts)
