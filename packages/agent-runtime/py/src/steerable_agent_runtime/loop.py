"""CoreLoop — the single-agent step loop (think → act → observe).

This is the A3 "minimal slice": the inner tool-round loop plus a completion
decision, yielding structured `LoopEvent`s (never encoded bytes). A
`TransportAdapter` encodes them for the wire; orchestration stays above.

Scope of this slice (see docs/spec/core-loop.md + CORELOOP_TODO.md A3):
  * inner loop state machine and round control
  * LLM stream consumption (via LLMProvider)
  * tool dispatch through the ToolExecutor port (dedup / policy / budget are
    loop-internal cross-cutting concerns)
  * budget counters + completion decision

Deliberately NOT in this slice (later slices per the plan): pseudo / markdown
tool-call recovery, soft-timeout, compaction-continue, large-result
externalization, and the anti-hallucination layer (data-need routing,
grounding judge, deferred/claimed retry, narration round).
"""

from __future__ import annotations

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

    Cross-cutting concerns (dedup, policy gate, budget) stay *in* the loop;
    an executor only runs the tool. The default implementation forwards to a
    `ToolRouter`; products inject handlers for UI tools, proposals, MCP, and
    (for the desktop) remote tools over the sidecar reverse channel.
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
    ) -> None:
        self._provider = provider
        self._executor = executor
        self._config = config or LoopConfig()
        # Compact trajectory recorded during run(); replayable via
        # replay.reduce_execution_state. Reset at the start of each run.
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
        self.trajectory = []

        def record(step: dict[str, Any], dec: CompletionDecision) -> None:
            self.trajectory.append(
                build_step_decision_event(step, _decision_data(dec))
            )

        yield LoopEvent("stage_start", {"model": self._provider.model})

        decision = CompletionDecision(status="failed", reason="loop did not run")
        for round_index in range(self._config.max_rounds):
            ctx.round_index = round_index

            # ── think: consume one LLM stream ────────────────────────────
            content_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            async for chunk in self._provider.stream(
                transcript,
                tools=tools,
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
                            "budget_exhausted", {"kind": "tokens", "used": budget_state.tokens_used}
                        )
                        decision = CompletionDecision(
                            status="budget_exhausted", reason="token budget exceeded"
                        )
                        record(
                            _step_summary(
                                round_index=round_index,
                                finish_reason="stop",
                                content="".join(content_parts),
                                tool_calls=tool_calls,
                                consecutive_tool_errors=ctx.consecutive_tool_errors,
                            ),
                            decision,
                        )
                        yield LoopEvent("completion", _decision_data(decision))
                        return

            content = "".join(content_parts)

            # ── recover pseudo / markdown tool calls ─────────────────────
            # Some models (local ones via Ollama, or Claude/OpenAI regressing
            # into prose) emit tool *intent* as text instead of a structured
            # tool_calls block. If this round produced no real tool_calls,
            # try to recover inline calls from the content so the act phase
            # runs instead of ending the turn tool-less. The cleaned text
            # (pseudo blocks removed) becomes the round's content.
            if not tool_calls and content:
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
                record(
                    _step_summary(
                        round_index=round_index,
                        finish_reason="stop",
                        content=content,
                        tool_calls=tool_calls,
                        consecutive_tool_errors=ctx.consecutive_tool_errors,
                    ),
                    decision,
                )
                yield LoopEvent("completion", _decision_data(decision))
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
                    record(
                        _step_summary(
                            round_index=round_index,
                            finish_reason="tool_calls",
                            content=content,
                            tool_calls=tool_calls,
                            consecutive_tool_errors=ctx.consecutive_tool_errors,
                        ),
                        decision,
                    )
                    yield LoopEvent("completion", _decision_data(decision))
                    return

            # ── observe: continue to next round ──────────────────────────
            decision = CompletionDecision(
                status="executing",
                reason="tool observations were produced; continue",
                confidence=0.7,
            )
            record(
                _step_summary(
                    round_index=round_index,
                    finish_reason="tool_calls",
                    content=content,
                    tool_calls=tool_calls,
                    consecutive_tool_errors=ctx.consecutive_tool_errors,
                ),
                decision,
            )
            yield LoopEvent("completion", _decision_data(decision))

        # Ran out of rounds — runaway guard.
        decision = CompletionDecision(
            status="budget_exhausted",
            reason=f"reached maxRounds={self._config.max_rounds} runaway guard",
            confidence=1.0,
        )
        record(
            _step_summary(
                round_index=self._config.max_rounds - 1,
                finish_reason="tool_calls",
                content="",
                tool_calls=[],
                consecutive_tool_errors=ctx.consecutive_tool_errors,
            ),
            decision,
        )
        yield LoopEvent(
            "budget_exhausted", {"kind": "rounds", "rounds": self._config.max_rounds}
        )
        yield LoopEvent("completion", _decision_data(decision))

        _ = started  # reserved for a future durationMs stage_complete event


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
