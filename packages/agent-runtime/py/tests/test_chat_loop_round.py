"""A1.2 round-body tests for ``ChatLoop``.

Covers:
* natural stop (assistant yields no tool calls → 1 round, no dispatch)
* single tool call → dispatch → 2 rounds (first with tool_call, second empty)
* multiple tool calls in one round dispatched in order
* tool_call_delta streamed across chunks → accumulated by id
* OpenAI-style id-only-on-first-chunk merging
* the 6 new hooks fire with correct ctx:
  before_round, before_send_messages, after_assistant_message,
  before_tool_call, after_tool_result, after_round
* ``before_send_messages`` mutations reach the provider
* ``before_tool_call`` returning ``HOOK_SKIP`` short-circuits dispatch and
  injects a synthetic tool result
* ``before_tool_call`` mutating ``tc_ctx.tool_call.arguments`` reaches dispatch
* ``max_rounds`` cap → final_status == "budget_exhausted"
* tool handler exceptions wrap into ``ToolResult(success=False, error=...)``
* per-round and aggregated ``LLMUsage``
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from typing import Any

import pytest

from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime import (
    HOOK_SKIP,
    AssistantMessageCtx,
    ChatLoop,
    LLMMessage,
    LLMStreamChunk,
    LLMUsage,
    LoopConfig,
    LoopEndCtx,
    RoundEndCtx,
    RoundStartCtx,
    SendMessagesCtx,
    ToolCallCtx,
    ToolResultCtx,
    ToolRouter,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class ScriptedProvider:
    """``LLMProvider`` whose ``stream()`` replays a scripted list of chunks per
    invocation.

    ``rounds`` is a list of chunk-lists; round N replays ``rounds[N]``. If the
    loop calls ``stream()`` more times than scripts exist, the extra calls get
    an empty stream (natural stop).

    Records every ``messages``/``tools``/``temperature``/``max_tokens`` it was
    called with under ``calls`` for assertion.
    """

    name = "scripted"
    model = "scripted-model"

    def __init__(self, rounds: list[list[LLMStreamChunk]]) -> None:
        self._rounds = rounds
        self._next = 0
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> tuple[LLMMessage, Any]:
        raise NotImplementedError("ScriptedProvider.complete() not used")

    async def stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        idx = self._next
        self._next += 1
        self.calls.append(
            {
                "messages": list(messages),
                "tools": list(tools) if tools else [],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if idx >= len(self._rounds):
            return
        for chunk in self._rounds[idx]:
            yield chunk


def _text_chunk(text: str) -> LLMStreamChunk:
    return LLMStreamChunk(content_delta=text)


def _tool_call_chunk(*, id: str, name: str, arguments: dict[str, Any]) -> LLMStreamChunk:
    return LLMStreamChunk(
        tool_call_delta=ToolCall(id=id, name=name, arguments=arguments)
    )


def _finish_chunk(reason: str = "stop", usage: LLMUsage | None = None) -> LLMStreamChunk:
    return LLMStreamChunk(finish_reason=reason, usage=usage)


def _make_router_with_tools() -> tuple[ToolRouter, dict[str, Any]]:
    """Build a router with a few well-known tools used by the tests.

    Returns the router and a dict of mutable observation state the tests can
    inspect (``calls`` list, ``args_seen`` list, etc.).
    """
    obs: dict[str, Any] = {"echo_calls": [], "add_calls": [], "boom_calls": []}
    router = ToolRouter()

    async def echo(text: str = "") -> dict[str, Any]:
        obs["echo_calls"].append(text)
        return {"echoed": text}

    async def add(a: int = 0, b: int = 0) -> ToolResult:
        obs["add_calls"].append((a, b))
        return ToolResult(success=True, message=f"sum={a + b}", data={"sum": a + b})

    async def boom() -> ToolResult:
        obs["boom_calls"].append(True)
        raise RuntimeError("intentional explosion")

    router.register(echo, description="Echo a string")
    router.register(add, description="Add two ints")
    router.register(boom, description="Always raises")
    return router, obs


def _make_config(
    provider: ScriptedProvider,
    router: ToolRouter,
    *,
    max_rounds: int = 12,
    initial_messages: Sequence[LLMMessage] | None = None,
    initial_state: dict[str, Any] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> LoopConfig:
    return LoopConfig(
        provider=provider,
        provider_kind="openai_compat",
        tool_router=router,
        initial_messages=initial_messages or [LLMMessage(role="user", content="hi")],
        initial_state=initial_state or {},
        max_rounds=max_rounds,
        temperature=temperature,
        max_tokens=max_tokens,
    )


# ---------------------------------------------------------------------------
# Natural stop / single round
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_natural_stop_after_one_round_with_no_tool_calls() -> None:
    router, _ = _make_router_with_tools()
    provider = ScriptedProvider(rounds=[[_text_chunk("hello!"), _finish_chunk()]])

    loop = ChatLoop(_make_config(provider, router))

    saw_end: dict[str, Any] = {}

    async def on_end(ctx: LoopEndCtx) -> None:
        saw_end["status"] = ctx.final_status
        saw_end["rounds"] = ctx.rounds_completed

    loop.on("loop_end", on_end)

    async for _ in loop.run():
        pass

    assert len(provider.calls) == 1, "should call provider exactly once"
    assert saw_end == {"status": "completed", "rounds": 1}


@pytest.mark.asyncio
async def test_single_tool_call_dispatched_and_loop_continues() -> None:
    router, obs = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            # Round 0: assistant requests echo("hi")
            [
                _text_chunk("calling tool..."),
                _tool_call_chunk(id="call_1", name="echo", arguments={"text": "hi"}),
                _finish_chunk("tool_calls"),
            ],
            # Round 1: assistant produces final text, no more tools
            [_text_chunk("done"), _finish_chunk("stop")],
        ]
    )
    loop = ChatLoop(_make_config(provider, router))

    saw_end: dict[str, Any] = {}

    async def on_end(ctx: LoopEndCtx) -> None:
        saw_end["status"] = ctx.final_status
        saw_end["rounds"] = ctx.rounds_completed

    loop.on("loop_end", on_end)

    async for _ in loop.run():
        pass

    assert len(provider.calls) == 2
    assert obs["echo_calls"] == ["hi"]
    assert saw_end == {"status": "completed", "rounds": 2}

    # Round 1 must include both the assistant message (with tool_calls) and the
    # tool result message — verifies the loop fed the result back.
    round1_msgs = provider.calls[1]["messages"]
    roles = [m.role for m in round1_msgs]
    assert roles == ["user", "assistant", "tool"], roles
    tool_msg = round1_msgs[-1]
    assert tool_msg.tool_call_id == "call_1"
    assert tool_msg.name == "echo"


@pytest.mark.asyncio
async def test_multiple_tool_calls_dispatched_in_order() -> None:
    router, obs = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            [
                _tool_call_chunk(id="a", name="add", arguments={"a": 1, "b": 2}),
                _tool_call_chunk(id="b", name="add", arguments={"a": 10, "b": 20}),
                _finish_chunk("tool_calls"),
            ],
            [_text_chunk("done"), _finish_chunk("stop")],
        ]
    )
    loop = ChatLoop(_make_config(provider, router))
    async for _ in loop.run():
        pass

    assert obs["add_calls"] == [(1, 2), (10, 20)]
    # Round 1 must contain assistant + two tool messages, in that order.
    round1_msgs = provider.calls[1]["messages"]
    roles = [m.role for m in round1_msgs]
    assert roles == ["user", "assistant", "tool", "tool"], roles
    assert round1_msgs[-2].tool_call_id == "a"
    assert round1_msgs[-1].tool_call_id == "b"


# ---------------------------------------------------------------------------
# tool_call_delta accumulation across chunks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_delta_chunks_merged_by_id() -> None:
    """Two chunks for the same id (name on chunk 1, args on chunk 2)."""
    router, obs = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            [
                # chunk 1: id + name, empty args
                _tool_call_chunk(id="c1", name="echo", arguments={}),
                # chunk 2: same id, args populated (provider's last-wins JSON
                # parse semantics)
                _tool_call_chunk(id="c1", name="echo", arguments={"text": "merged"}),
                _finish_chunk("tool_calls"),
            ],
            [_finish_chunk("stop")],
        ]
    )
    loop = ChatLoop(_make_config(provider, router))
    async for _ in loop.run():
        pass

    assert obs["echo_calls"] == ["merged"]


@pytest.mark.asyncio
async def test_tool_call_delta_with_empty_id_continues_last_call() -> None:
    """OpenAI convention: id only on first chunk, subsequent chunks have id=''."""
    router, obs = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            [
                _tool_call_chunk(id="c1", name="echo", arguments={}),
                _tool_call_chunk(id="", name="", arguments={"text": "continued"}),
                _finish_chunk("tool_calls"),
            ],
            [_finish_chunk("stop")],
        ]
    )
    loop = ChatLoop(_make_config(provider, router))
    async for _ in loop.run():
        pass

    assert obs["echo_calls"] == ["continued"]


# ---------------------------------------------------------------------------
# All 6 new hooks fire with correct ctx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_round_hooks_fire_in_order_with_correct_ctx() -> None:
    router, _ = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            [
                _text_chunk("doing math"),
                _tool_call_chunk(id="call_1", name="add", arguments={"a": 2, "b": 3}),
                _finish_chunk(
                    "tool_calls",
                    usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                ),
            ],
            [
                _text_chunk("done"),
                _finish_chunk(
                    "stop",
                    usage=LLMUsage(prompt_tokens=20, completion_tokens=2, total_tokens=22),
                ),
            ],
        ]
    )
    loop = ChatLoop(_make_config(provider, router))

    events: list[str] = []
    captured: dict[str, Any] = {"send_messages_rounds": [], "round_usages": []}

    async def on_before_round(ctx: RoundStartCtx) -> None:
        events.append(f"before_round[{ctx.round_index}]")
        assert ctx.tools, "tool descriptors must be populated"

    async def on_before_send(ctx: SendMessagesCtx) -> None:
        events.append(f"before_send[{len(ctx.messages)}]")
        captured["send_messages_rounds"].append(len(ctx.messages))
        assert ctx.model == "scripted-model"
        assert ctx.provider_kind == "openai_compat"

    async def on_after_assistant(ctx: AssistantMessageCtx) -> None:
        events.append("after_assistant")
        assert ctx.message is not None
        assert ctx.message.role == "assistant"
        captured["round_usages"].append(ctx.usage)

    async def on_before_tc(ctx: ToolCallCtx) -> None:
        events.append(f"before_tc[{ctx.tool_call.name}]")

    async def on_after_tr(ctx: ToolResultCtx) -> None:
        events.append(f"after_tr[{ctx.tool_result.success}]")
        assert ctx.tool_call is not None
        assert ctx.tool_result is not None

    async def on_after_round(ctx: RoundEndCtx) -> None:
        events.append(
            f"after_round[{ctx.round_index},calls={len(ctx.tool_calls)},results={len(ctx.tool_results)}]"
        )

    async def on_end(ctx: LoopEndCtx) -> None:
        events.append(
            f"loop_end[{ctx.final_status},rounds={ctx.rounds_completed},"
            f"total={(ctx.total_usage or {}).get('total_tokens', 0)}]"
        )

    loop.on("before_round", on_before_round)
    loop.on("before_send_messages", on_before_send)
    loop.on("after_assistant_message", on_after_assistant)
    loop.on("before_tool_call", on_before_tc)
    loop.on("after_tool_result", on_after_tr)
    loop.on("after_round", on_after_round)
    loop.on("loop_end", on_end)

    async for _ in loop.run():
        pass

    # Round 0: before_round → before_send → after_assistant → before_tc → after_tr → after_round
    # Round 1: before_round → before_send → after_assistant → after_round (no tool calls)
    # Then: loop_end
    assert events == [
        "before_round[0]",
        "before_send[1]",  # only the initial user msg
        "after_assistant",
        "before_tc[add]",
        "after_tr[True]",
        "after_round[0,calls=1,results=1]",
        "before_round[1]",
        "before_send[3]",  # user + assistant + tool
        "after_assistant",
        "after_round[1,calls=0,results=0]",
        "loop_end[completed,rounds=2,total=37]",
    ], events

    assert captured["round_usages"] == [
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22},
    ]


# ---------------------------------------------------------------------------
# before_send_messages mutates payload before LLM call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_send_messages_can_inject_system_prompt() -> None:
    router, _ = _make_router_with_tools()
    provider = ScriptedProvider(rounds=[[_finish_chunk("stop")]])
    loop = ChatLoop(_make_config(provider, router, temperature=0.7))

    async def inject(ctx: SendMessagesCtx) -> None:
        ctx.messages.insert(0, LLMMessage(role="system", content="be terse"))
        ctx.temperature = 0.1
        ctx.max_tokens = 256

    loop.on("before_send_messages", inject)

    async for _ in loop.run():
        pass

    sent = provider.calls[0]
    assert [m.role for m in sent["messages"]] == ["system", "user"]
    assert sent["messages"][0].content == "be terse"
    assert sent["temperature"] == 0.1
    assert sent["max_tokens"] == 256


# ---------------------------------------------------------------------------
# before_tool_call: HOOK_SKIP + arg mutation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_tool_call_hook_skip_short_circuits_dispatch() -> None:
    router, obs = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            [
                _tool_call_chunk(id="c1", name="echo", arguments={"text": "do not run"}),
                _finish_chunk("tool_calls"),
            ],
            [_finish_chunk("stop")],
        ]
    )
    loop = ChatLoop(_make_config(provider, router))

    async def block(ctx: ToolCallCtx) -> Any:
        if ctx.tool_call.name == "echo":
            return HOOK_SKIP
        return None

    loop.on("before_tool_call", block)

    async for _ in loop.run():
        pass

    assert obs["echo_calls"] == [], "tool must not have been dispatched"

    # The synthetic tool result must still appear in round 1's messages so the
    # model sees a tool message per outstanding call.
    round1_msgs = provider.calls[1]["messages"]
    tool_msgs = [m for m in round1_msgs if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "c1"
    assert "skipped" in tool_msgs[0].content.lower()


@pytest.mark.asyncio
async def test_before_tool_call_can_mutate_arguments() -> None:
    router, obs = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            [
                _tool_call_chunk(id="c1", name="add", arguments={"a": 1, "b": 1}),
                _finish_chunk("tool_calls"),
            ],
            [_finish_chunk("stop")],
        ]
    )
    loop = ChatLoop(_make_config(provider, router))

    async def double_args(ctx: ToolCallCtx) -> None:
        ctx.tool_call.arguments = {"a": 100, "b": 200}

    loop.on("before_tool_call", double_args)

    async for _ in loop.run():
        pass

    assert obs["add_calls"] == [(100, 200)]


# ---------------------------------------------------------------------------
# max_rounds cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_rounds_cap_yields_budget_exhausted() -> None:
    """An infinite tool-call loop must hit max_rounds and exit."""
    router, _ = _make_router_with_tools()

    # Every round demands another tool call; loop should give up at max_rounds.
    rounds = [
        [
            _tool_call_chunk(id=f"c{i}", name="add", arguments={"a": 1, "b": 1}),
            _finish_chunk("tool_calls"),
        ]
        for i in range(10)
    ]
    provider = ScriptedProvider(rounds=rounds)
    loop = ChatLoop(_make_config(provider, router, max_rounds=3))

    seen: dict[str, Any] = {}

    async def on_end(ctx: LoopEndCtx) -> None:
        seen["status"] = ctx.final_status
        seen["rounds"] = ctx.rounds_completed

    loop.on("loop_end", on_end)

    async for _ in loop.run():
        pass

    assert seen == {"status": "budget_exhausted", "rounds": 3}
    assert len(provider.calls) == 3


# ---------------------------------------------------------------------------
# Tool handler exception wrapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_handler_exception_wraps_into_failed_result() -> None:
    router, obs = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            [
                _tool_call_chunk(id="boom1", name="boom", arguments={}),
                _finish_chunk("tool_calls"),
            ],
            [_finish_chunk("stop")],
        ]
    )
    loop = ChatLoop(_make_config(provider, router))

    captured: dict[str, Any] = {}

    async def on_tr(ctx: ToolResultCtx) -> None:
        captured["success"] = ctx.tool_result.success
        captured["error"] = ctx.tool_result.error

    loop.on("after_tool_result", on_tr)

    async for _ in loop.run():
        pass

    assert obs["boom_calls"] == [True]
    # ToolRouter already wraps the exception into ToolResult(success=False, …)
    # before the ChatLoop sees it — both the router and the loop sanity-check
    # this path. The result must still flow through the after_tool_result hook.
    assert captured["success"] is False
    assert captured["error"] is not None
    assert "explosion" in captured["error"]


# ---------------------------------------------------------------------------
# Done event still bracketed by session envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_envelope_unchanged_with_round_body() -> None:
    router, _ = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            [
                _tool_call_chunk(id="c1", name="echo", arguments={"text": "x"}),
                _finish_chunk("tool_calls"),
            ],
            [_text_chunk("done"), _finish_chunk("stop")],
        ]
    )
    loop = ChatLoop(_make_config(provider, router))

    events = [e async for e in loop.run()]
    # A1.4 will add round-level SSE events; A1.2 still emits only the
    # session envelope: session.start → done → session.end
    assert [(e.type, e.event) for e in events] == [
        ("agent", "session.start"),
        ("done", None),
        ("agent", "session.end"),
    ]


# ---------------------------------------------------------------------------
# Review fixes: reasoning, finish_reason, dynamic tools, empty-id warning,
# final_decision placeholder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_delta_accumulated_into_assistant_ctx() -> None:
    """``reasoning_delta`` chunks must surface on ``AssistantMessageCtx.reasoning``."""
    router, _ = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            [
                LLMStreamChunk(reasoning_delta="Let me think... "),
                LLMStreamChunk(reasoning_delta="actually, "),
                LLMStreamChunk(reasoning_delta="just respond."),
                _text_chunk("Hi!"),
                _finish_chunk("stop"),
            ],
        ]
    )
    loop = ChatLoop(_make_config(provider, router))

    seen: dict[str, Any] = {}

    async def capture(ctx: AssistantMessageCtx) -> None:
        seen["reasoning"] = ctx.reasoning
        seen["content"] = ctx.message.content if ctx.message else None

    loop.on("after_assistant_message", capture)

    async for _ in loop.run():
        pass

    assert seen["reasoning"] == "Let me think... actually, just respond."
    assert seen["content"] == "Hi!"


@pytest.mark.asyncio
async def test_reasoning_is_none_when_no_reasoning_delta() -> None:
    """No ``reasoning_delta`` → ctx.reasoning is None, not empty string."""
    router, _ = _make_router_with_tools()
    provider = ScriptedProvider(rounds=[[_text_chunk("hi"), _finish_chunk("stop")]])
    loop = ChatLoop(_make_config(provider, router))

    captured: list[Any] = []

    async def cb(ctx: AssistantMessageCtx) -> None:
        captured.append(ctx.reasoning)

    loop.on("after_assistant_message", cb)

    async for _ in loop.run():
        pass

    assert captured == [None]


@pytest.mark.asyncio
async def test_finish_reason_propagated_to_round_end_ctx() -> None:
    """``finish_reason`` from the LLM stream must reach ``RoundEndCtx.finish_reason``."""
    router, _ = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            [
                _tool_call_chunk(id="c1", name="echo", arguments={"text": "x"}),
                _finish_chunk("tool_calls"),
            ],
            [_text_chunk("d"), _finish_chunk("length")],  # length cutoff
        ]
    )
    loop = ChatLoop(_make_config(provider, router))

    reasons: list[str | None] = []

    async def cb(ctx: RoundEndCtx) -> None:
        reasons.append(ctx.finish_reason)

    loop.on("after_round", cb)

    async for _ in loop.run():
        pass

    assert reasons == ["tool_calls", "length"]


@pytest.mark.asyncio
async def test_before_round_can_extend_tools_for_subsequent_rounds() -> None:
    """``ctx.tools.append(...)`` in before_round must persist across rounds.

    Verifies I4: tools is a working buffer, not a per-round snapshot.
    """
    router, _ = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            [
                _tool_call_chunk(id="c1", name="echo", arguments={"text": "x"}),
                _finish_chunk("tool_calls"),
            ],
            [_finish_chunk("stop")],
        ]
    )
    loop = ChatLoop(_make_config(provider, router))

    extra_descriptor = {
        "type": "function",
        "function": {"name": "dynamic_tool", "description": "added at runtime", "parameters": {}},
    }

    fired = 0

    async def extend_tools(ctx: RoundStartCtx) -> None:
        nonlocal fired
        fired += 1
        if fired == 1:
            ctx.tools.append(extra_descriptor)

    loop.on("before_round", extend_tools)

    async for _ in loop.run():
        pass

    # Round 0 sees 3 router tools (echo/add/boom); the hook appends 1 more.
    # Round 1 must still see all 4 because the buffer survives.
    round0_tools = provider.calls[0]["tools"]
    round1_tools = provider.calls[1]["tools"]
    round0_names = [t["function"]["name"] for t in round0_tools]
    round1_names = [t["function"]["name"] for t in round1_tools]

    assert "dynamic_tool" in round0_names
    assert "dynamic_tool" in round1_names, (
        "tool extension in before_round must persist to subsequent rounds"
    )


@pytest.mark.asyncio
async def test_empty_id_tool_call_delta_with_empty_acc_drops_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Provider quirk: a delta with empty id arriving before any call is open
    must be dropped with a WARNING — not crash, not silently corrupt state."""
    import logging

    router, obs = _make_router_with_tools()
    provider = ScriptedProvider(
        rounds=[
            [
                # First delta has empty id — provider bug. Loop should drop it.
                _tool_call_chunk(id="", name="echo", arguments={"text": "ghost"}),
                # Then a legit call arrives.
                _tool_call_chunk(id="real", name="echo", arguments={"text": "ok"}),
                _finish_chunk("tool_calls"),
            ],
            [_finish_chunk("stop")],
        ]
    )
    loop = ChatLoop(_make_config(provider, router))

    with caplog.at_level(logging.WARNING, logger="steerable_agent_runtime.chat_loop"):
        async for _ in loop.run():
            pass

    # The ghost delta must NOT have produced a phantom dispatch.
    assert obs["echo_calls"] == ["ok"]
    # And we must have logged a warning about it.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("empty id" in r.getMessage() for r in warnings), (
        f"expected a warning about empty-id delta, got: {[r.getMessage() for r in warnings]}"
    )


@pytest.mark.asyncio
async def test_loop_end_ctx_final_decision_populated_for_natural_stop() -> None:
    """A1.3: ``final_decision`` carries the ``CompletionDecision.to_dict()``
    that ended the loop. For a natural stop (no tool_calls), the decision is
    ``status=completed, reason=no_tool_calls``."""
    router, _ = _make_router_with_tools()
    provider = ScriptedProvider(rounds=[[_finish_chunk("stop")]])
    loop = ChatLoop(_make_config(provider, router))

    seen: dict[str, Any] = {}

    async def cb(ctx: LoopEndCtx) -> None:
        seen["final_status"] = ctx.final_status
        seen["final_decision"] = ctx.final_decision

    loop.on("loop_end", cb)

    async for _ in loop.run():
        pass

    assert seen["final_status"] == "completed"
    assert seen["final_decision"] is not None
    assert seen["final_decision"]["status"] == "completed"
    assert seen["final_decision"]["reason"] == "no_tool_calls"
    assert seen["final_decision"]["limit_kind"] is None
    assert seen["final_decision"]["terminal_index"] is None
