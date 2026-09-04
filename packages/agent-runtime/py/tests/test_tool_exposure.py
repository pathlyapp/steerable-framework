"""Wave 2 step 3: tool exposure tiers — registration ≠ exposure.

Three tiers (codex's ``Direct / Deferred / Hidden`` converged on the same
shape): ``direct`` tools are model-visible, ``deferred`` tools are
dispatchable and discoverable through ``tool_search`` but omitted from the
offered list, ``hidden`` tools are dispatch-only. Dispatch never gates on
exposure, so a discovered deferred tool calls without being re-listed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall

from steerable_agent_runtime import (
    CoreLoop,
    LLMMessage,
    RouterToolExecutor,
    ToolRouter,
    register_tool_search,
    tool,
    tool_search_descriptor,
)
from steerable_agent_runtime.harness_spec import (
    assemble_harness,
    harness_spec_from_dict,
)
from steerable_agent_runtime.llm import LLMStreamChunk, LLMUsage


def _msg(role: str, text: str) -> LLMMessage:
    return LLMMessage.text_of(role, text)  # type: ignore[arg-type]


def _provider(script: list[dict[str, Any]]):
    class _FakeProvider:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self.calls: list[list[LLMMessage]] = []
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            self.calls.append(list(messages))
            entry = script[min(self._idx, len(script) - 1)]
            self._idx += 1

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                if entry.get("content"):
                    yield LLMStreamChunk(content_delta=entry["content"])
                for call in entry.get("tool_calls", []):
                    yield LLMStreamChunk(tool_call_delta=call)
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop",
                    usage=LLMUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
                )

            return _gen()

    return _FakeProvider()


async def _collect(loop_run: AsyncIterator) -> list[Any]:
    return [e async for e in loop_run]


def _names(descriptors: list[dict[str, Any]]) -> list[str]:
    return [d["function"]["name"] for d in descriptors]


# ---------------------------------------------------------------------------
# Tier metadata and listing
# ---------------------------------------------------------------------------


def test_default_exposure_is_direct() -> None:
    router = ToolRouter()

    @tool(router=router, description="Echo text")
    async def echo(text: str) -> str:
        return text

    registered = router.get("echo")
    assert registered is not None
    assert registered.exposure == "direct"


def test_describe_model_omits_deferred_and_hidden() -> None:
    router = ToolRouter()
    router.register(lambda: "a", name="direct_tool", exposure="direct")
    router.register(lambda: "b", name="deferred_tool", exposure="deferred")
    router.register(lambda: "c", name="hidden_tool", exposure="hidden")

    assert _names(router.describe_model()) == ["direct_tool"]
    # describe() stays the full inventory for host introspection.
    assert sorted(_names(router.describe())) == [
        "deferred_tool",
        "direct_tool",
        "hidden_tool",
    ]
    assert [t.name for t in router.deferred_tools()] == ["deferred_tool"]


def test_decorator_and_remote_exposure_passthrough() -> None:
    router = ToolRouter()

    @tool(router=router, exposure="deferred", description="Deferred via decorator")
    async def decorated() -> str:
        return "d"

    async def invoker(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {"success": True}

    router.register_remote("remote_hidden", invoker, exposure="hidden")

    assert router.get("decorated").exposure == "deferred"  # type: ignore[union-attr]
    assert router.get("remote_hidden").exposure == "hidden"  # type: ignore[union-attr]
    assert _names(router.describe_model()) == []


# ---------------------------------------------------------------------------
# Dispatch is exposure-agnostic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_runs_deferred_and_hidden_tools() -> None:
    router = ToolRouter()
    router.register(lambda: "deferred-result", name="d_tool", exposure="deferred")
    router.register(lambda: "hidden-result", name="h_tool", exposure="hidden")

    deferred = await router.dispatch(ToolCall(id="1", name="d_tool", arguments={}))
    hidden = await router.dispatch(ToolCall(id="2", name="h_tool", arguments={}))

    assert deferred.success and deferred.data["value"] == "deferred-result"
    assert hidden.success and hidden.data["value"] == "hidden-result"


@pytest.mark.asyncio
async def test_unknown_tool_error_hides_hidden_tools() -> None:
    router = ToolRouter()
    router.register(lambda: "a", name="visible_tool", exposure="direct")
    router.register(lambda: "b", name="findable_tool", exposure="deferred")
    router.register(lambda: "c", name="secret_tool", exposure="hidden")

    result = await router.dispatch(ToolCall(id="1", name="zzz", arguments={}))

    assert not result.success
    assert "visible_tool" in result.error
    assert "findable_tool" in result.error
    assert "secret_tool" not in result.error


# ---------------------------------------------------------------------------
# tool_search discovery seam
# ---------------------------------------------------------------------------


def _searchable_router() -> ToolRouter:
    router = ToolRouter()
    router.register(
        lambda: "a",
        name="direct_tool",
        description="A directly exposed tool",
    )
    router.register(
        lambda: "b",
        name="mcp__github__create_issue",
        description="Create a GitHub issue in a repository",
        exposure="deferred",
    )
    router.register(
        lambda: "c",
        name="mcp__github__list_prs",
        description="List pull requests in a GitHub repository",
        exposure="deferred",
    )
    router.register(
        lambda: "d",
        name="mcp__linear__create_issue",
        description="Create a Linear issue",
        exposure="deferred",
    )
    router.register(
        lambda: "e",
        name="internal_reset",
        description="Reset internal state",
        exposure="hidden",
    )
    return router


@pytest.mark.asyncio
async def test_tool_search_finds_deferred_by_keyword() -> None:
    router = _searchable_router()
    register_tool_search(router)

    result = await router.dispatch(
        ToolCall(id="1", name="tool_search", arguments={"query": "github"})
    )

    assert result.success
    matches = result.data["value"]["matches"]
    names = [m["name"] for m in matches]
    assert "mcp__github__create_issue" in names
    assert "mcp__github__list_prs" in names
    assert "mcp__linear__create_issue" not in names
    # Full schema rides along so a match is callable without another lookup.
    assert matches[0]["parameters"] is not None
    assert result.data["value"]["deferredCount"] == 3


@pytest.mark.asyncio
async def test_tool_search_ranks_name_hits_first() -> None:
    router = ToolRouter()
    router.register(
        lambda: "a",
        name="deploy_app",
        description="Deploy the app",
        exposure="deferred",
    )
    router.register(
        lambda: "b",
        name="run_checks",
        description="Run the deploy pipeline checks",
        exposure="deferred",
    )
    register_tool_search(router)

    result = await router.dispatch(
        ToolCall(id="1", name="tool_search", arguments={"query": "deploy"})
    )

    # Name tokens index double, so the name hit outranks the
    # description-only hit; both rank (BM25 needs one term, not all).
    names = [m["name"] for m in result.data["value"]["matches"]]
    assert names == ["deploy_app", "run_checks"]


@pytest.mark.asyncio
async def test_tool_search_tolerates_off_vocabulary_terms() -> None:
    router = _searchable_router()
    register_tool_search(router)

    result = await router.dispatch(
        ToolCall(
            id="1", name="tool_search", arguments={"query": "github frobnicate"}
        )
    )

    # "frobnicate" hits nothing; the terms that do hit still rank.
    names = {m["name"] for m in result.data["value"]["matches"]}
    assert names == {"mcp__github__create_issue", "mcp__github__list_prs"}


@pytest.mark.asyncio
async def test_tool_search_scores_partial_term_matches() -> None:
    router = _searchable_router()
    register_tool_search(router)

    result = await router.dispatch(
        ToolCall(id="1", name="tool_search", arguments={"query": "create issue"})
    )

    # list_prs contains neither term and scores 0 — the relevance floor
    # keeps it out without requiring every term to hit.
    names = {m["name"] for m in result.data["value"]["matches"]}
    assert names == {"mcp__github__create_issue", "mcp__linear__create_issue"}


@pytest.mark.asyncio
async def test_tool_search_default_cap_is_eight() -> None:
    router = ToolRouter()
    for i in range(10):
        router.register(
            lambda: "x",
            name=f"crm_action_{i}",
            description="CRM action",
            exposure="deferred",
        )
    register_tool_search(router)

    result = await router.dispatch(
        ToolCall(id="1", name="tool_search", arguments={"query": "crm"})
    )

    assert len(result.data["value"]["matches"]) == 8
    # The descriptor advertises the same default the handler applies.
    params = tool_search_descriptor()["function"]["parameters"]
    assert "default 8" in params["properties"]["max_results"]["description"]


@pytest.mark.asyncio
async def test_tool_search_per_call_ceiling_is_20() -> None:
    router = ToolRouter()
    for i in range(25):
        router.register(
            lambda: "x",
            name=f"crm_action_{i}",
            description="CRM action",
            exposure="deferred",
        )
    register_tool_search(router)

    result = await router.dispatch(
        ToolCall(
            id="1",
            name="tool_search",
            arguments={"query": "crm", "max_results": 100},
        )
    )

    # Every match carries a full schema; the ceiling bounds the payload no
    # matter what a call requests.
    assert len(result.data["value"]["matches"]) == 20


@pytest.mark.asyncio
async def test_tool_search_respects_max_results() -> None:
    router = _searchable_router()
    register_tool_search(router, max_results=1)

    result = await router.dispatch(
        ToolCall(id="1", name="tool_search", arguments={"query": "github"})
    )
    assert len(result.data["value"]["matches"]) == 1

    per_call = await router.dispatch(
        ToolCall(
            id="2",
            name="tool_search",
            arguments={"query": "github", "max_results": 2},
        )
    )
    assert len(per_call.data["value"]["matches"]) == 2


@pytest.mark.asyncio
async def test_tool_search_no_match_and_hidden_invisible() -> None:
    router = _searchable_router()
    register_tool_search(router)

    result = await router.dispatch(
        ToolCall(id="1", name="tool_search", arguments={"query": "nonexistent"})
    )
    assert result.data["value"]["matches"] == []
    assert "No deferred tools matched" in result.data["value"]["note"]

    hidden = await router.dispatch(
        ToolCall(id="2", name="tool_search", arguments={"query": "reset internal"})
    )
    assert hidden.data["value"]["matches"] == []


# ---------------------------------------------------------------------------
# progressive harness strategy: router wiring + tier-derived selection
# ---------------------------------------------------------------------------


def _harness_for(tools_impl: str):
    return assemble_harness(
        harness_spec_from_dict(
            {
                "context": "null",
                "retry": "none",
                "validator": "null",
                "tools": tools_impl,
                "memory": "stateless",
                "orchestration": "single",
            }
        )
    )


def test_progressive_select_without_router_fails_loud() -> None:
    """Selecting progressive without wiring the router raises instead of
    offering a tool_search descriptor nothing can dispatch."""
    harness = _harness_for("progressive")
    with pytest.raises(ValueError, match="wire_tools"):
        harness.select_tools([{"type": "function", "function": {"name": "bash"}}])


def test_wire_tools_is_a_noop_for_filter_strategies() -> None:
    """full/minimal bind nothing: wiring must not conjure a search tool."""
    router = ToolRouter()
    harness = _harness_for("full")
    harness.wire_tools(router)
    assert router.get("tool_search") is None
    tools = [{"type": "function", "function": {"name": "bash"}}]
    assert harness.select_tools(tools) == tools


@pytest.mark.asyncio
async def test_progressive_offers_direct_tier_and_dispatchable_search() -> None:
    router = ToolRouter()
    router.register(lambda text: text, name="echo", description="Echo text")
    router.register(
        lambda city: {"weather": city},
        name="get_weather",
        description="Get current weather for a city",
        schema={
            "type": "object",
            "properties": {"city": {"type": "string"}},
        },
        exposure="deferred",
    )
    router.register(
        lambda: "x",
        name="internal_reset",
        description="Reset internal state",
        exposure="hidden",
    )

    harness = _harness_for("progressive")
    harness.wire_tools(router)
    # Selection derives from the registry's tiers even when handed the full
    # inventory: deferred/hidden are unlisted, exactly one search descriptor.
    offered = harness.select_tools(router.describe())
    assert _names(offered) == ["echo", "tool_search"]
    # Selection is idempotent — wiring already put tool_search in the
    # direct tier, so re-selecting the offered list changes nothing.
    assert _names(harness.select_tools(offered)) == ["echo", "tool_search"]

    # The offered search tool dispatches and finds the deferred tool.
    found = await router.dispatch(
        ToolCall(id="1", name="tool_search", arguments={"query": "weather"})
    )
    assert found.success
    assert [m["name"] for m in found.data["value"]["matches"]] == ["get_weather"]

    # Hidden tools are in neither the listing nor search results.
    hidden = await router.dispatch(
        ToolCall(id="2", name="tool_search", arguments={"query": "reset internal"})
    )
    assert hidden.data["value"]["matches"] == []

    # The deferred tool dispatches directly by name without being listed.
    direct = await router.dispatch(
        ToolCall(id="3", name="get_weather", arguments={"city": "Paris"})
    )
    assert direct.success


# ---------------------------------------------------------------------------
# Loop integration: discover a deferred tool, then call it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_discovers_and_calls_deferred_tool() -> None:
    router = ToolRouter()

    @tool(router=router, description="Echo text")
    async def echo(text: str) -> dict[str, str]:
        return {"echo": text}

    router.register(
        lambda city: {"weather": f"sunny in {city}"},
        name="get_weather",
        description="Get current weather for a city",
        schema={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
        exposure="deferred",
    )
    register_tool_search(router)

    provider = _provider(
        [
            {"tool_calls": [ToolCall(id="s1", name="tool_search", arguments={"query": "weather"})]},
            {"tool_calls": [ToolCall(id="w1", name="get_weather", arguments={"city": "Paris"})]},
            {"content": "It is sunny in Paris."},
        ]
    )
    loop = CoreLoop(provider, RouterToolExecutor(router))
    # register_tool_search puts the discovery tool in the direct tier, so
    # describe_model() is the whole offered list; the deferred tool is not
    # in it. (Hosts building the list externally append
    # tool_search_descriptor() instead — the sidecar composition pattern.)
    tools = router.describe_model()
    assert _names(tools) == ["echo", "tool_search"]

    events = await _collect(loop.run([_msg("user", "weather in Paris?")], tools=tools))

    kinds = [e.kind for e in events]
    # Two tool rounds (search, then the discovered call), then completion.
    assert kinds.count("stage_complete") == 2
    assert events[-1].kind == "completion"
    # The deferred tool executed: its result reached the transcript.
    tool_messages = [
        m
        for call in provider.calls
        for m in call
        if m.role == "tool" and m.name == "get_weather"
    ]
    assert tool_messages, "deferred tool result should reach the model"
    assert "sunny in Paris" in tool_messages[0].content_text
