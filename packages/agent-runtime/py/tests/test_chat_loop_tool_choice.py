"""ChatLoop tool_choice plumbing tests.

Covers:

* ``LoopConfig.tool_choice`` reaches the provider on the very first round.
* ``LoopConfig.tool_choice`` is re-emitted every round by default (no
  hook = same choice every call). This matches the documented "applied
  to every round" contract.
* A ``before_send_messages`` hook can rewrite ``ctx.tool_choice`` per
  round — typical Coordinator pattern: force a tool call on round 0,
  release the constraint on round 1+ so the model can summarise tool
  results.
* Default behaviour with no ``tool_choice`` set: the provider receives
  no ``tool_choice`` kwarg (downstream wire format omits the field).
* "required" / specific-function / "none" all pass through unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from typing import Any

import pytest

from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime import (
    ChatLoop,
    LLMMessage,
    LLMStreamChunk,
    LoopConfig,
    SendMessagesCtx,
    ToolRouter,
)


# Sentinel: pytest doesn't know we treat "no key recorded" vs "recorded
# as None" differently, so use a private object.
_MISSING = object()


class RecordingProvider:
    """Captures every ``stream`` invocation's ``tool_choice`` (or _MISSING).

    Yields a single ``finish_chunk`` so each round completes naturally.
    """

    name = "recording"
    model = "rec-model"

    def __init__(self, rounds: int) -> None:
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
        raise NotImplementedError

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
                "round_index": idx,
                "tool_choice": kwargs.get("tool_choice", _MISSING),
                "tool_choice_in_kwargs": "tool_choice" in kwargs,
            }
        )
        if idx == 0 and self._rounds > 1:
            # Force one tool call so the loop continues to round 1.
            yield LLMStreamChunk(
                tool_call_delta=ToolCall(id="c0", name="ping", arguments={})
            )
            yield LLMStreamChunk(finish_reason="tool_calls")
        else:
            yield LLMStreamChunk(content_delta="done")
            yield LLMStreamChunk(finish_reason="stop")


def _make_router() -> ToolRouter:
    router = ToolRouter()

    async def ping() -> ToolResult:
        return ToolResult(success=True, message="pong", data={})

    router.register(ping, description="ping")
    return router


def _make_config(
    provider: RecordingProvider,
    router: ToolRouter,
    *,
    tool_choice: Any = None,
) -> LoopConfig:
    return LoopConfig(
        provider=provider,
        provider_kind="openai_compat",
        tool_router=router,
        initial_messages=[LLMMessage(role="user", content="plan it")],
        initial_state={},
        max_rounds=5,
        tool_choice=tool_choice,
    )


# ---------------------------------------------------------------------------
# Default — no tool_choice set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_tool_choice_not_passed_to_provider() -> None:
    provider = RecordingProvider(rounds=1)
    loop = ChatLoop(_make_config(provider, _make_router()))

    async for _ in loop.run():
        pass

    # When tool_choice is None at LoopConfig, the loop must not pass the
    # kwarg to provider.stream — otherwise providers that do not know
    # the field would fail.
    assert provider.calls[0]["tool_choice_in_kwargs"] is False


# ---------------------------------------------------------------------------
# Constant tool_choice — applied every round
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_constant_required_tool_choice_applies_each_round() -> None:
    provider = RecordingProvider(rounds=2)
    loop = ChatLoop(
        _make_config(provider, _make_router(), tool_choice="required"),
    )

    async for _ in loop.run():
        pass

    assert len(provider.calls) == 2
    assert provider.calls[0]["tool_choice"] == "required"
    # Default policy: re-applies on every round unless a hook intervenes.
    assert provider.calls[1]["tool_choice"] == "required"


@pytest.mark.asyncio
async def test_specific_function_tool_choice_propagates_verbatim() -> None:
    provider = RecordingProvider(rounds=1)
    choice = {"type": "function", "function": {"name": "ping"}}
    loop = ChatLoop(
        _make_config(provider, _make_router(), tool_choice=choice),
    )

    async for _ in loop.run():
        pass

    assert provider.calls[0]["tool_choice"] == choice


@pytest.mark.asyncio
async def test_tool_choice_none_string_propagates() -> None:
    provider = RecordingProvider(rounds=1)
    loop = ChatLoop(
        _make_config(provider, _make_router(), tool_choice="none"),
    )

    async for _ in loop.run():
        pass

    assert provider.calls[0]["tool_choice"] == "none"


# ---------------------------------------------------------------------------
# Hook rewrites tool_choice per round (Coordinator-style first-round-only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_can_clear_tool_choice_for_followup_rounds() -> None:
    """Coordinator pattern: force ``make_plan`` on round 0, drop the
    constraint on round 1 so the model can summarise the tool result.
    """
    provider = RecordingProvider(rounds=2)
    forced = {"type": "function", "function": {"name": "ping"}}
    loop = ChatLoop(
        _make_config(provider, _make_router(), tool_choice=forced),
    )

    async def release_after_round_zero(ctx: SendMessagesCtx) -> None:
        if ctx.round_index > 0:
            ctx.tool_choice = None

    loop.on("before_send_messages", release_after_round_zero)

    async for _ in loop.run():
        pass

    assert len(provider.calls) == 2
    assert provider.calls[0]["tool_choice"] == forced
    # Round 1's hook cleared the choice; the loop must respect the
    # mutated ctx value, NOT re-read LoopConfig.
    assert provider.calls[1]["tool_choice_in_kwargs"] is False


@pytest.mark.asyncio
async def test_hook_can_set_tool_choice_when_loopconfig_default() -> None:
    """Inverse: LoopConfig default is None; hook adds tool_choice on a
    later round (e.g. after detecting a planning failure mid-stream).
    """
    provider = RecordingProvider(rounds=2)
    loop = ChatLoop(_make_config(provider, _make_router(), tool_choice=None))

    async def force_on_round_one(ctx: SendMessagesCtx) -> None:
        if ctx.round_index == 1:
            ctx.tool_choice = "required"

    loop.on("before_send_messages", force_on_round_one)

    async for _ in loop.run():
        pass

    assert len(provider.calls) == 2
    assert provider.calls[0]["tool_choice_in_kwargs"] is False
    assert provider.calls[1]["tool_choice"] == "required"
