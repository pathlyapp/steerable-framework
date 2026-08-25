"""CoreLoop — the single-agent step loop (think → act → observe).

This is the A3 "minimal slice": the inner tool-round loop plus a completion
decision, yielding structured `LoopEvent`s (never encoded bytes). A
`TransportAdapter` encodes them for the wire; orchestration stays above.

Implemented so far (see docs/spec/core-loop.md + CORELOOP_TODO.md A3):
  * inner loop state machine and round control
  * LLM stream consumption (via LLMProvider)
  * tool dispatch through the ToolExecutor port
  * token budget counters + completion decision
  * pseudo / markdown tool-call recovery (see pseudo.py)
  * LoopHooks extension points (see hooks.py): pre_step / post_tool_result /
    on_request_error — remaining slices land as hook implementations, not as
    more branches here
  * single write path: completion events carry their full step summary and
    the compact trajectory is derived from them (no separate record channel)
  * soft timeout: a wall-clock limit (LoopConfig.soft_timeout_ms) checked at
    round boundaries; once exceeded the loop stops offering tools and asks
    the model for a final answer instead of hard-killing the run

Not yet implemented (later slices per the plan): tool dedup, policy gate,
and the anti-hallucination layer (data-need routing, grounding judge,
deferred/claimed retry, narration round). Compaction and large-result
externalization live in hooks (compaction.py / spill.py), not here.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from steerable_agent_harness import (
    BudgetLimit,
    BudgetState,
    consume_budget,
)
from steerable_agent_protocol.generated import ToolCall, ToolResult

from .hooks import LoopHooks, NoopHooks
from .llm import LLMMessage, LLMProvider
from .pseudo import extract_inline_tool_calls
from .replay import (
    HarnessTrajectoryEvent,
    build_step_decision_event,
)
from .tools import ToolRouter

# ---------------------------------------------------------------------------
# LoopEvent
# ---------------------------------------------------------------------------

#: Event categories per docs/spec/core-loop.md. The *kind* is framework-owned;
#: the *data* payload may carry product fields (consumers ignore unknowns).
LoopEventKind = Literal[
    # lifecycle
    "stage_start",
    "stage_complete",
    "error",
    # content stream
    "content_delta",
    "reasoning_delta",
    # tool side
    "tool_call_start",
    "tool_call_result",
    "tool_error",
    # budget / control
    "budget_exhausted",
    "soft_timeout",
    "completion",
]


@dataclass(slots=True)
class LoopEvent:
    """A structured event yielded by the loop. Never encoded bytes."""

    kind: LoopEventKind
    data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Completion decision
# ---------------------------------------------------------------------------

CompletionStatus = Literal["executing", "completed", "failed", "budget_exhausted"]


@dataclass(slots=True)
class CompletionDecision:
    status: CompletionStatus
    reason: str
    confidence: float = 1.0


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


@runtime_checkable
class ToolExecutor(Protocol):
    """Dispatch port for tool calls.

    An executor only runs the tool; cross-cutting concerns (dedup, policy gate,
    budget) belong in the loop — of those only the token budget is wired up so
    far. The default implementation forwards to a `ToolRouter`; products inject
    handlers for UI tools, proposals, MCP, and (for the desktop) remote tools
    over the sidecar reverse channel.
    """

    async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult: ...


class RouterToolExecutor:
    """Default ToolExecutor: dispatch through an in-process ToolRouter."""

    def __init__(self, router: ToolRouter, *, consent_granted: bool = False) -> None:
        self._router = router
        self._consent_granted = consent_granted

    async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
        return await self._router.dispatch(
            call,
            consent_granted=self._consent_granted,
            context={"chat_id": ctx.chat_id, "round": ctx.round_index},
        )


# ---------------------------------------------------------------------------
# Context + config
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LoopContext:
    """Per-run state threaded through the loop and exposed to executors."""

    chat_id: str | None = None
    round_index: int = 0
    tool_calls_used: int = 0
    consecutive_tool_errors: int = 0


@dataclass(slots=True)
class LoopConfig:
    """Tunables for one loop run.

    `max_tool_errors` uses CONSECUTIVE semantics (reset on success) — see
    docs/spec/core-loop.md "Known semantic divergences": deeppath-agent counts
    consecutive, deeppath-api counts cumulative; CoreLoop standardizes on
    consecutive and makes the threshold configurable.
    """

    max_rounds: int = 32
    max_tool_errors: int = 3
    budget: BudgetLimit | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    #: Wall-clock soft limit. When exceeded, the loop stops offering tools and
    #: asks the model to wrap up with what it has (one final no-tools round),
    #: instead of hard-killing the run. ``None`` disables.
    soft_timeout_ms: int | None = None


# ---------------------------------------------------------------------------
# CoreLoop
# ---------------------------------------------------------------------------


class CoreLoop:
    """Minimal single-agent step loop.

    Usage::

        loop = CoreLoop(provider, executor, config)
        async for event in loop.run(messages):
            transport.emit(encode(event))  # encoding is the adapter's job

    The loop owns the message transcript: it appends the assistant message and
    each tool result so the next round sees fresh observations.
    """

    def __init__(
        self,
        provider: LLMProvider,
        executor: ToolExecutor,
        config: LoopConfig | None = None,
        hooks: LoopHooks | None = None,
    ) -> None:
        self._provider = provider
        self._executor = executor
        self._config = config or LoopConfig()
        self._hooks: LoopHooks = hooks if hooks is not None else NoopHooks()
        # Compact trajectory recorded during run(); replayable via
        # replay.reduce_execution_state. Derived from the completion events
        # (single write path — see _emit_completion). Reset each run.
        self.trajectory: list[HarnessTrajectoryEvent] = []

    async def run(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        chat_id: str | None = None,
    ) -> AsyncIterator[LoopEvent]:
        ctx = LoopContext(chat_id=chat_id)
        transcript: list[LLMMessage] = list(messages)
        budget_state = BudgetState()
        started = time.monotonic()
        soft_deadline = (
            started + self._config.soft_timeout_ms / 1000
            if self._config.soft_timeout_ms is not None
            else None
        )
        wrap_up = False
        self.trajectory = []

        def emit_completion(
            step: dict[str, Any], dec: CompletionDecision
        ) -> LoopEvent:
            """Build the completion event carrying its full step summary, and
            derive the trajectory entry from it (single write path — the
            trajectory is never recorded through a separate channel, so the
            event stream alone can rebuild it)."""
            data = {**step, **_decision_data(dec)}
            self.trajectory.append(build_step_decision_event(step, _decision_data(dec)))
            return LoopEvent("completion", data)

        def step_summary(
            *,
            round_index: int,
            finish_reason: str,
            content: str,
            tool_calls: list[ToolCall],
        ) -> dict[str, Any]:
            return _step_summary(
                round_index=round_index,
                finish_reason=finish_reason,
                content=content,
                tool_calls=tool_calls,
                consecutive_tool_errors=ctx.consecutive_tool_errors,
            )

        yield LoopEvent("stage_start", {"model": self._provider.model})

        decision = CompletionDecision(status="failed", reason="loop did not run")
        for round_index in range(self._config.max_rounds):
            ctx.round_index = round_index

            # ── soft timeout: stop offering tools, ask for the wrap-up ────
            # Soft = we never interrupt an in-flight stream or tool; the
            # deadline is only checked at round boundaries.
            if (
                soft_deadline is not None
                and not wrap_up
                and time.monotonic() >= soft_deadline
            ):
                wrap_up = True
                yield LoopEvent(
                    "soft_timeout",
                    {
                        "round": round_index,
                        "elapsedMs": int((time.monotonic() - started) * 1000),
                        "softTimeoutMs": self._config.soft_timeout_ms,
                    },
                )
                transcript.append(
                    LLMMessage(role="user", content=_SOFT_TIMEOUT_NOTICE)
                )

            # ── hook: pre_step (compaction / turn rejection) ─────────────
            pre = await self._hooks.pre_step(transcript, ctx)
            if pre.kind == "reject":
                decision = CompletionDecision(
                    status="failed",
                    reason=pre.reason or "rejected by pre_step hook",
                    confidence=1.0,
                )
                yield emit_completion(
                    step_summary(
                        round_index=round_index,
                        finish_reason="stop",
                        content="",
                        tool_calls=[],
                    ),
                    decision,
                )
                return
            if pre.transcript is not None:
                transcript = pre.transcript

            # ── think: consume one LLM stream (retry via hook) ───────────
            content_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            while True:
                try:
                    async for chunk in self._provider.stream(
                        transcript,
                        # wrap-up round: withhold tool descriptors so the model
                        # cannot start another act phase.
                        tools=None if wrap_up else tools,
                        temperature=self._config.temperature,
                        max_tokens=self._config.max_tokens,
                    ):
                        if chunk.content_delta:
                            content_parts.append(chunk.content_delta)
                            yield LoopEvent("content_delta", {"delta": chunk.content_delta})
                        if chunk.reasoning_delta:
                            yield LoopEvent("reasoning_delta", {"delta": chunk.reasoning_delta})
                        if chunk.tool_call_delta is not None:
                            tool_calls.append(chunk.tool_call_delta)
                        if chunk.usage is not None and self._config.budget is not None:
                            budget_state, exhausted = consume_budget(
                                budget_state, self._config.budget, tokens=chunk.usage.total_tokens
                            )
                            if exhausted:
                                yield LoopEvent(
                                    "budget_exhausted",
                                    {"kind": "tokens", "used": budget_state.tokens_used},
                                )
                                decision = CompletionDecision(
                                    status="budget_exhausted", reason="token budget exceeded"
                                )
                                yield emit_completion(
                                    step_summary(
                                        round_index=round_index,
                                        finish_reason="stop",
                                        content="".join(content_parts),
                                        tool_calls=tool_calls,
                                    ),
                                    decision,
                                )
                                return
                    break  # stream completed without error
                except Exception as exc:  # noqa: BLE001 — hook decides retry/fail
                    action = await self._hooks.on_request_error(exc, ctx)
                    if action.kind == "retry":
                        if action.delay_ms > 0:
                            await asyncio.sleep(action.delay_ms / 1000)
                        continue
                    yield LoopEvent(
                        "error",
                        {"message": str(exc), "round": round_index, "phase": "llm_stream"},
                    )
                    decision = CompletionDecision(
                        status="failed",
                        reason=action.reason or f"llm stream error: {exc}",
                        confidence=0.9,
                    )
                    yield emit_completion(
                        step_summary(
                            round_index=round_index,
                            finish_reason="stop",
                            content="".join(content_parts),
                            tool_calls=tool_calls,
                        ),
                        decision,
                    )
                    return

            content = "".join(content_parts)

            # ── recover pseudo / markdown tool calls ─────────────────────
            # Some models (local ones via Ollama, or Claude/OpenAI regressing
            # into prose) emit tool *intent* as text instead of a structured
            # tool_calls block. If this round produced no real tool_calls,
            # try to recover inline calls from the content so the act phase
            # runs instead of ending the turn tool-less. The cleaned text
            # (pseudo blocks removed) becomes the round's content.
            # Skipped in wrap-up mode: tools are no longer offered, and any
            # tool intent (structured or pseudo) is dropped so the turn ends.
            if wrap_up:
                tool_calls = []
            elif not tool_calls and content:
                recovered, cleaned = extract_inline_tool_calls(content)
                if recovered:
                    content = cleaned
                    tool_calls = [
                        ToolCall(
                            id=f"recovered_{round_index}_{i}",
                            name=r["name"],
                            arguments=r["arguments"],
                        )
                        for i, r in enumerate(recovered)
                    ]

            # ── decide: no tool calls → terminal ─────────────────────────
            if not tool_calls:
                if content.strip():
                    decision = CompletionDecision(
                        status="completed",
                        reason="assistant produced final response with no pending tools",
                        confidence=0.85,
                    )
                else:
                    decision = CompletionDecision(
                        status="failed",
                        reason="no tool calls and no final response",
                        confidence=0.75,
                    )
                yield emit_completion(
                    step_summary(
                        round_index=round_index,
                        finish_reason="stop",
                        content=content,
                        tool_calls=tool_calls,
                    ),
                    decision,
                )
                return

            # ── act: append assistant turn, then run each tool ───────────
            transcript.append(
                LLMMessage(role="assistant", content=content, tool_calls=tool_calls)
            )

            for call in tool_calls:
                yield LoopEvent(
                    "tool_call_start", {"id": call.id, "name": call.name, "round": round_index}
                )
                tool_started = time.monotonic()
                try:
                    result = await self._executor.execute(call, ctx)
                except Exception as exc:  # noqa: BLE001 — surface as tool_error event
                    ctx.consecutive_tool_errors += 1
                    yield LoopEvent(
                        "tool_error",
                        {"id": call.id, "name": call.name, "error": str(exc)},
                    )
                    result = ToolResult(success=False, error=str(exc), needsFollowup=True)
                else:
                    if result.success:
                        ctx.consecutive_tool_errors = 0
                    else:
                        ctx.consecutive_tool_errors += 1

                # ── hook: post_tool_result (spill / truncation) ──────────
                result = await self._hooks.post_tool_result(result, call, ctx)

                ctx.tool_calls_used += 1
                duration_ms = int((time.monotonic() - tool_started) * 1000)
                yield LoopEvent(
                    "tool_call_result",
                    {
                        "id": call.id,
                        "name": call.name,
                        "success": result.success,
                        "durationMs": duration_ms,
                    },
                )
                transcript.append(
                    LLMMessage(
                        role="tool",
                        name=call.name,
                        tool_call_id=call.id,
                        content=_result_content(result),
                    )
                )

                # consecutive-error breaker (runaway guard)
                if ctx.consecutive_tool_errors >= self._config.max_tool_errors:
                    decision = CompletionDecision(
                        status="failed",
                        reason="too many consecutive tool errors",
                        confidence=0.9,
                    )
                    yield emit_completion(
                        step_summary(
                            round_index=round_index,
                            finish_reason="tool_calls",
                            content=content,
                            tool_calls=tool_calls,
                        ),
                        decision,
                    )
                    return

            # ── observe: continue to next round ──────────────────────────
            decision = CompletionDecision(
                status="executing",
                reason="tool observations were produced; continue",
                confidence=0.7,
            )
            yield emit_completion(
                step_summary(
                    round_index=round_index,
                    finish_reason="tool_calls",
                    content=content,
                    tool_calls=tool_calls,
                ),
                decision,
            )

        # Ran out of rounds — runaway guard.
        decision = CompletionDecision(
            status="budget_exhausted",
            reason=f"reached maxRounds={self._config.max_rounds} runaway guard",
            confidence=1.0,
        )
        yield LoopEvent(
            "budget_exhausted", {"kind": "rounds", "rounds": self._config.max_rounds}
        )
        yield emit_completion(
            step_summary(
                round_index=self._config.max_rounds - 1,
                finish_reason="tool_calls",
                content="",
                tool_calls=[],
            ),
            decision,
        )

        _ = started  # reserved for a future durationMs stage_complete event


_SOFT_TIMEOUT_NOTICE = (
    "[system notice] The time budget for this task is exhausted. Do NOT call "
    "any more tools. Summarize what you have done so far and produce the "
    "final answer now."
)


def _decision_data(decision: CompletionDecision) -> dict[str, Any]:
    return {
        "status": decision.status,
        "reason": decision.reason,
        "confidence": decision.confidence,
    }


def _step_summary(
    *,
    round_index: int,
    finish_reason: str,
    content: str,
    tool_calls: list[ToolCall],
    consecutive_tool_errors: int,
) -> dict[str, Any]:
    """Build the compact step summary recorded into the trajectory.

    Field names align with the api/agent contract (round, traceStepId,
    finishReason, toolCalls, toolCallCount, toolErrorCount, textLength).
    """

    return {
        "round": round_index,
        "traceStepId": f"round_{round_index}",
        "finishReason": finish_reason,
        "textLength": len(content),
        "toolCalls": [c.name for c in tool_calls],
        "toolCallCount": len(tool_calls),
        "toolErrorCount": consecutive_tool_errors,
    }


def _result_content(result: ToolResult) -> str:
    """Serialize a ToolResult into the tool-message content for the transcript."""

    import json

    payload: dict[str, Any] = {"success": result.success}
    if result.error:
        payload["error"] = result.error
    if result.data is not None:
        payload["data"] = result.data
    if result.message:
        payload["message"] = result.message
    return json.dumps(payload, ensure_ascii=False)
