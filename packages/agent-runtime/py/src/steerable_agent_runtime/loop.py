"""CoreLoop — the single-agent step loop (think → act → observe).

This is the A3 "minimal slice": the inner tool-round loop plus a completion
decision, yielding structured `LoopEvent`s (never encoded bytes). A
`TransportAdapter` encodes them for the wire; orchestration stays above.

Implemented so far (see docs/spec/core-loop.md + CORELOOP_TODO.md A3):
  * inner loop state machine and round control
  * LLM stream consumption (via LLMProvider) with display hygiene:
    UTF-16 surrogate-pair carry and streaming pseudo/echo-block stripping
    (see pseudo.py) — raw text is kept for recovery/transcript, cleaned
    text is what ``content_delta`` events emit
  * tool dispatch through the ToolExecutor port, with hygiene guards:
    same-turn ``(name, args)`` dedup (soft ``duplicate_call`` signal),
    unknown-tool suggestions and schema argument coercion (tools.py)
  * token budget counters + completion decision
  * pseudo / markdown tool-call recovery (see pseudo.py)
  * LoopHooks extension points (see hooks.py): pre_step (declared rewrite /
    appends + tool_choice) / post_tool_result / on_request_error /
    before_completion (terminal veto: discipline retry or narration round) —
    capabilities land as hook implementations, not as more branches here
  * single write path: completion events carry their full step summary and
    the compact trajectory is derived from them (no separate record channel)
  * typed append-only history (see history.py): the transcript the provider
    sees is a projection of the record; every mutation is an append, and
    hook rewrites go through the declared ``ContextManager.replace_all``
    path (the only rewrite, itself append-only)
  * soft timeout: a wall-clock limit (LoopConfig.soft_timeout_ms) checked at
    round boundaries; once exceeded the loop stops offering tools and asks
    the model for a final answer instead of hard-killing the run
  * per-tool timeout (LoopConfig.tool_timeout_ms): a hung tool returns a
    failed ToolResult instead of hanging the turn; the consecutive-error
    breaker treats it like any other tool failure

The anti-hallucination layer (data-need routing, grounding judge,
deferred/claimed retry, narration round) lives in antihallucination.py as a
LoopHooks implementation. Compaction and large-result externalization live
in hooks (compaction.py / spill.py), not here. Observability lives in
tracing.py (a TraceRecorder consuming this event stream), not in the loop
itself.
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

from .history import ContextFragment, ContextManager
from .hooks import CompletionAction, CompletionDraft, LoopHooks, NoopHooks
from .llm import LLMMessage, LLMProvider
from .pseudo import (
    PseudoStreamStripper,
    extract_inline_tool_calls,
    split_trailing_high_surrogate,
    strip_pseudo_fn_final,
)
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
    # mid-turn user steering (see CoreLoop.steer)
    "steer",
    # hook-driven control flow (compaction, retry, narration, tool_choice) —
    # emitted at the decision point so traces show *why* the loop changed
    # course; without it hook triggers are invisible to offline analysis.
    "hook_action",
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

    Optional duck-typed method: ``concurrency_safe(call) -> bool``. When
    present and ``LoopConfig.parallel_tools`` is on, consecutive safe calls
    in one round run concurrently. Absent → serial (safe default).
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

    def concurrency_safe(self, call: ToolCall) -> bool:
        """Optional hook the loop uses for parallel batching (duck-typed —
        executors without it are treated as serial-only)."""
        tool = self._router.get(call.name)
        return bool(tool and tool.concurrency_safe)


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
    #: Successful tool results this turn — the anti-hallucination layer keys
    #: off "zero usable tool returns" to detect fabricated data reports.
    tool_successes: int = 0
    #: Provider-reported prompt tokens of the last completed request, and the
    #: transcript length at that moment. Ground truth for compaction pressure
    #: (the heuristic estimate drifts per model; this does not). Hooks that
    #: rewrite the transcript must reset both — the indices go stale.
    last_prompt_tokens: int | None = None
    last_prompt_transcript_len: int = 0


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
    #: Block re-issuing an identical ``(name, args)`` call within one run.
    #: Deterministic tools return identical output for identical input, so a
    #: repeat only burns tokens and can push the model into a retry loop
    #: (ported from deeppath-api's P0.3 guard; counts toward the consecutive
    #: tool-error breaker). No write/destructive exemption — idempotency of
    #: side effects belongs to the action layer below.
    tool_dedup: bool = True
    #: Include the full tool result in ``tool_call_result`` events (not just
    #: the 300-char preview). Off by default to keep traces small; enable when
    #: the trace is the resume record (see ``resume.project_transcript``).
    persist_tool_results: bool = False
    #: Run consecutive concurrency-safe tool calls from the same round
    #: concurrently (asyncio.gather); unsafe calls form a barrier and run
    #: alone. A call is safe when the executor says so — RouterToolExecutor
    #: looks up ``RegisteredTool.concurrency_safe``; executors without the
    #: check (e.g. HostToolExecutor, which serializes on the host) stay
    #: serial. Event order stays deterministic: start events in call order,
    #: result events in call order after each batch completes.
    parallel_tools: bool = True
    #: Per-tool-execution wall-clock limit. ``soft_timeout_ms`` is only
    #: checked at round boundaries, so without this a hung tool (a dead
    #: remote server, a stuck reverse-channel call) hangs the whole turn.
    #: On expiry the call returns a failed ``ToolResult`` (error
    #: ``tool_timeout``) instead of raising through the loop, so the
    #: consecutive-error breaker handles it like any other tool failure.
    #: Applies to every executor, in-process or remote. The default is a
    #: backstop against *hung* tools, not a budget — products with fast
    #: tools should set a tighter value. ``None`` disables.
    tool_timeout_ms: int | None = 300_000

    def __post_init__(self) -> None:
        if self.tool_timeout_ms is not None and self.tool_timeout_ms <= 0:
            raise ValueError("tool_timeout_ms must be positive (or None to disable)")


# ---------------------------------------------------------------------------
# CoreLoop
# ---------------------------------------------------------------------------


class CoreLoop:
    """Minimal single-agent step loop.

    Usage::

        loop = CoreLoop(provider, executor, config)
        async for event in loop.run(messages):
            transport.emit(encode(event))  # encoding is the adapter's job

    The loop owns the model-visible record (a typed, append-only
    ``ContextManager`` log — see history.py): every transcript mutation is
    an append, and hook rewrites go through the declared ``replace_all``
    path, so each round's projection reflects everything recorded so far.
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
        # Mid-turn user messages land here via steer() and are drained into
        # the transcript at the next round boundary (dsh-style "inject":
        # consumed at the next step, no separate wakeup semantics).
        self._inbox: asyncio.Queue[str] = asyncio.Queue()
        # Compact trajectory recorded during run(); replayable via
        # replay.reduce_execution_state. Derived from the completion events
        # (single write path — see _emit_completion). Reset each run.
        self.trajectory: list[HarnessTrajectoryEvent] = []
        # The append-only model-visible record for the current run (the
        # transcript is its projection). Rebuilt per run() from the seed
        # messages; exposed for tests, persistence, and resume.
        self.history = ContextManager()

    def steer(self, content: str) -> None:
        """Inject a user message into a running turn.

        Called from the same event loop (e.g. a sidecar RPC handler) while
        ``run()`` is active; the message is appended to the transcript at the
        next round boundary and surfaced as a ``steer`` event. Messages sent
        after the run ends are ignored by the (already closed) consumer.
        """
        if content:
            self._inbox.put_nowait(content)

    async def _execute_tool(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
        """Run one tool call under the per-tool timeout.

        A timeout returns a failed ``ToolResult`` rather than raising, so the
        call flows through the normal result path (post_tool_result hooks,
        transcript append, consecutive-error breaker) like any other tool
        failure. The wrapped coroutine is cancelled on expiry; a remote
        executor's late reply is dropped by its own pending-call table (see
        ``JsonRpcServer._resolve_reverse_response``), so the turn is never
        blocked by a hung peer again.
        """

        timeout_ms = self._config.tool_timeout_ms
        if timeout_ms is None:
            return await self._executor.execute(call, ctx)
        try:
            return await asyncio.wait_for(
                self._executor.execute(call, ctx), timeout=timeout_ms / 1000
            )
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error="tool_timeout",
                needsFollowup=True,
                data={
                    "timeout": True,
                    "timeoutMs": timeout_ms,
                    "message": _TOOL_TIMEOUT_MESSAGE.format(
                        name=call.name, timeout_ms=timeout_ms
                    ),
                },
            )

    async def _offer_narration(
        self,
        content: str,
        round_index: int,
        *,
        had_tool_calls: bool,
        ctx: LoopContext,
        wrap_up: bool,
        completion_redos: int,
    ) -> tuple[str, str | None] | None:
        """Offer a terminal draft to ``before_completion`` for narration.

        Returns ``(narration prompt, reason)`` to seed a wrap-up round, or
        ``None`` to proceed with the terminal emit. Only empty-content
        terminals qualify (a turn that already produced text needs no
        summary), never during an in-flight wrap-up, and redo budget is
        enforced by the caller's count.
        """

        if wrap_up or content.strip() or completion_redos >= _MAX_COMPLETION_REDOS:
            return None
        action = await self._hooks.before_completion(
            CompletionDraft(
                status="failed",
                reason="terminal with no natural-language content",
                content=content,
                round_index=round_index,
                had_tool_calls=had_tool_calls,
                tool_calls_used=ctx.tool_calls_used,
                tool_successes=ctx.tool_successes,
            ),
            ctx,
        )
        if action.kind == "narrate":
            return (action.message or _NARRATION_REQUEST, action.reason)
        return None

    async def run(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        chat_id: str | None = None,
    ) -> AsyncIterator[LoopEvent]:
        ctx = LoopContext(chat_id=chat_id)
        # The transcript is a projection of the append-only record; nothing
        # mutates a list in place anymore (see history.py).
        manager = ContextManager(messages, token_model=self._provider.model)
        self.history = manager
        budget_state = BudgetState()
        tool_call_signatures: set[tuple[str, str]] = set()
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

        def record_terminal_content(content: str, tool_calls: list[ToolCall]) -> None:
            """Append the terminal assistant message to the record.

            Rounds that produced tool calls recorded their assistant message
            in the act phase; only no-tool-call terminals (the final answer,
            or partial content on an error/budget exit) are missing. The
            append happens after the last provider request of the run, so
            request bytes are unaffected — the record simply becomes
            complete enough to resume from.
            """
            if tool_calls or not content.strip():
                return
            manager.append(LLMMessage.text_of("assistant", content))

        yield LoopEvent("stage_start", {"model": self._provider.model})

        decision = CompletionDecision(status="failed", reason="loop did not run")
        round_index = 0
        # Discipline retries / narration rounds granted by before_completion do
        # not consume the round budget (mirrors the TS loop's `turn -= 1`), but
        # the loop still caps them so a faulty hook cannot spin forever.
        completion_redos = 0
        while True:
            ctx.round_index = round_index

            # ── steer: drain mid-turn user injections into the transcript ──
            # Drained before hooks so pre_step (compaction, routing) sees the
            # same transcript the provider will receive.
            while not self._inbox.empty():
                injected = self._inbox.get_nowait()
                manager.append(
                    LLMMessage.text_of("user", injected), kind="steer.inject"
                )
                yield LoopEvent(
                    "steer", {"content": injected, "round": round_index}
                )

            # ── runaway guard: round budget exhausted ────────────────────
            # A terminal like any other: offer narration (the model may have
            # produced only tool calls), then emit if no hook intervenes.
            if round_index >= self._config.max_rounds and not wrap_up:
                decision = CompletionDecision(
                    status="budget_exhausted",
                    reason=f"reached maxRounds={self._config.max_rounds} runaway guard",
                    confidence=1.0,
                )
                yield LoopEvent(
                    "budget_exhausted",
                    {"kind": "rounds", "rounds": self._config.max_rounds},
                )
                narrate = await self._offer_narration(
                    "", round_index, had_tool_calls=True, ctx=ctx,
                    wrap_up=wrap_up, completion_redos=completion_redos,
                )
                if narrate is not None:
                    prompt, reason = narrate
                    completion_redos += 1
                    wrap_up = True
                    manager.append_fragment(NarrationRequest(prompt))
                    yield LoopEvent(
                        "hook_action",
                        {
                            "hook": "before_completion",
                            "action": "narrate",
                            "reason": reason or "budget-exhausted narration",
                            "round": round_index,
                        },
                    )
                    continue
                yield emit_completion(
                    step_summary(
                        round_index=round_index - 1,
                        finish_reason="tool_calls",
                        content="",
                        tool_calls=[],
                    ),
                    decision,
                )
                return

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
                manager.append_fragment(SoftTimeoutNotice())

            # ── hook: pre_step (compaction / turn rejection / tool_choice) ──
            # Hooks receive a throwaway projection list and return
            # declarations; the record can only change through the manager
            # (append / declared replace_all), applied here by the loop.
            projection = manager.projection
            pre = await self._hooks.pre_step(projection, ctx)
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
            if pre.rewrite is not None:
                manager.replace_all(
                    pre.rewrite.messages,
                    reason=pre.rewrite.reason,
                    action=pre.rewrite.action,
                )
                yield LoopEvent(
                    "hook_action",
                    {
                        "hook": "pre_step",
                        "action": pre.rewrite.action,
                        "reason": pre.rewrite.reason,
                        "round": round_index,
                    },
                )
            if pre.appends:
                for item in pre.appends:
                    manager.append(item.message, kind=item.kind)
                yield LoopEvent(
                    "hook_action",
                    {
                        "hook": "pre_step",
                        "action": pre.append_action or "append",
                        "reason": pre.reason or "context appended",
                        "round": round_index,
                    },
                )
            # tool_choice only makes sense when tools are actually offered.
            step_tool_choice = (
                pre.tool_choice if (pre.tool_choice and tools and not wrap_up) else None
            )
            if step_tool_choice:
                yield LoopEvent(
                    "hook_action",
                    {
                        "hook": "pre_step",
                        "action": "tool_choice",
                        "value": step_tool_choice,
                        "round": round_index,
                    },
                )

            # ── think: consume one LLM stream (retry via hook) ───────────
            content_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            # Display pipeline: raw text is accumulated for recovery and the
            # transcript, but content_delta events pass through a surrogate
            # carry (half-emoji split across chunks) and the pseudo stripper
            # (echo blocks / pseudo calls never reach the user's screen).
            stripper = PseudoStreamStripper()
            content_carry = ""
            reasoning_carry = ""
            while True:
                try:
                    async for chunk in self._provider.stream(
                        manager.projection,
                        # wrap-up round: withhold tool descriptors so the model
                        # cannot start another act phase.
                        tools=None if wrap_up else tools,
                        temperature=self._config.temperature,
                        max_tokens=self._config.max_tokens,
                        **(
                            {"tool_choice": step_tool_choice}
                            if step_tool_choice
                            else {}
                        ),
                    ):
                        if chunk.content_delta:
                            content_parts.append(chunk.content_delta)
                            emit, content_carry = split_trailing_high_surrogate(
                                chunk.content_delta, content_carry
                            )
                            display = stripper.feed(emit)
                            if display:
                                yield LoopEvent("content_delta", {"delta": display})
                        if chunk.reasoning_delta:
                            emit, reasoning_carry = split_trailing_high_surrogate(
                                chunk.reasoning_delta, reasoning_carry
                            )
                            if emit:
                                yield LoopEvent("reasoning_delta", {"delta": emit})
                        if chunk.tool_call_delta is not None:
                            tool_calls.append(chunk.tool_call_delta)
                        if chunk.usage is not None:
                            # Ground-truth context size for compaction pressure.
                            ctx.last_prompt_tokens = chunk.usage.prompt_tokens
                            ctx.last_prompt_transcript_len = len(manager.projection)
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
                                record_terminal_content("".join(content_parts), tool_calls)
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
                    projection = manager.projection
                    action = await self._hooks.on_request_error(exc, projection, ctx)
                    if action.kind == "retry":
                        yield LoopEvent(
                            "hook_action",
                            {
                                "hook": "on_request_error",
                                "action": "retry",
                                "reason": action.reason or str(exc),
                                "delayMs": action.delay_ms,
                                # True when the hook declared a transcript
                                # rewrite for the retry (context-overflow
                                # recovery).
                                "compacted": action.rewrite is not None,
                                "round": round_index,
                            },
                        )
                        # The retry re-streams from scratch: drop the failed
                        # attempt's partial content and display-pipeline state
                        # so neither the transcript nor the user sees it twice.
                        content_parts = []
                        tool_calls = []
                        stripper = PseudoStreamStripper()
                        content_carry = ""
                        reasoning_carry = ""
                        # A hook may declare a transcript rewrite for the
                        # retry (context-overflow recovery compacts first) —
                        # the declared replace_all path records the boundary.
                        if action.rewrite is not None:
                            manager.replace_all(
                                action.rewrite.messages,
                                reason=action.rewrite.reason,
                                action=action.rewrite.action,
                            )
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
                    record_terminal_content("".join(content_parts), tool_calls)
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

            # Flush the display pipeline: any held-back tail (partial marker
            # that never completed, deferred surrogate half) goes out now.
            if content_carry:
                tail = stripper.feed(content_carry)
                if tail:
                    yield LoopEvent("content_delta", {"delta": tail})
            tail = stripper.flush()
            if tail:
                yield LoopEvent("content_delta", {"delta": tail})
            if reasoning_carry:
                yield LoopEvent("reasoning_delta", {"delta": reasoning_carry})

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

            # Belt-and-suspenders: drop any echo blocks that slipped past the
            # streaming filter before the content enters the transcript.
            content = strip_pseudo_fn_final(content)

            # ── decide: no tool calls → terminal (unless a hook vetoes) ──
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
                # A wrap-up round IS the final answer — offering a NON-empty
                # wrap-up back to before_completion would loop (its content
                # is exactly what a discipline retry would flag). An EMPTY
                # wrap-up is different: the model glitched on the narration
                # ask itself, so give hooks one bounded second chance
                # (AntiHallucinationHooks caps narrations at 2).
                if (
                    (not wrap_up or not content.strip())
                    and completion_redos < _MAX_COMPLETION_REDOS
                ):
                    action = await self._hooks.before_completion(
                        CompletionDraft(
                            status=decision.status,
                            reason=decision.reason,
                            content=content,
                            round_index=round_index,
                            had_tool_calls=False,
                            tool_calls_used=ctx.tool_calls_used,
                            tool_successes=ctx.tool_successes,
                        ),
                        ctx,
                    )
                    if action.kind == "retry":
                        # Send the "all talk, no tool_call" round back: the
                        # assistant text goes on the record so the model sees
                        # what it said, then the discipline notice corrects it.
                        completion_redos += 1
                        if content.strip():
                            manager.append(LLMMessage.text_of("assistant", content))
                        manager.append_fragment(DisciplineRetryNotice(action.message))
                        yield LoopEvent(
                            "hook_action",
                            {
                                "hook": "before_completion",
                                "action": "retry",
                                "reason": action.reason or "before_completion retry",
                                "round": round_index,
                            },
                        )
                        yield LoopEvent(
                            "stage_complete",
                            {
                                "round": round_index,
                                "disciplineRetry": True,
                                "reason": action.reason or "before_completion retry",
                            },
                        )
                        continue
                    if action.kind == "narrate":
                        completion_redos += 1
                        wrap_up = True
                        manager.append_fragment(NarrationRequest(action.message))
                        yield LoopEvent(
                            "hook_action",
                            {
                                "hook": "before_completion",
                                "action": "narrate",
                                "reason": action.reason or "before_completion narrate",
                                "round": round_index,
                            },
                        )
                        continue
                record_terminal_content(content, tool_calls)
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
            manager.append(
                LLMMessage.text_of("assistant", content, tool_calls=tool_calls)
            )

            # Parallel batching: consecutive concurrency-safe calls run under
            # one asyncio.gather; any other call is a barrier batch of one.
            # Determinism is preserved for consumers — start events emit in
            # call order before execution, result events in call order after
            # the batch completes (durations stay per-call).
            concurrency_check = getattr(self._executor, "concurrency_safe", None)
            batches: list[tuple[bool, list[ToolCall]]] = []
            for call in tool_calls:
                safe = bool(
                    self._config.parallel_tools
                    and concurrency_check is not None
                    and concurrency_check(call)
                )
                if safe and batches and batches[-1][0]:
                    batches[-1][1].append(call)
                else:
                    batches.append((safe, [call]))

            breaker_tripped = False
            for batch_idx, (batch_safe, batch) in enumerate(batches):
                if breaker_tripped:
                    break

                # Phase 1 (sequential): start events + dedup guard. The dedup
                # guard skips execution for an identical (name, args) call
                # that already ran this run, feeding back a soft "you already
                # called this" signal; it is checked before the executor so
                # retried *unknown* tools are blocked too (their signature is
                # recorded on the first attempt).
                # NOTE: results/errors are keyed by batch position, not
                # call.id — providers have been seen emitting duplicate ids.
                pending: list[tuple[int, ToolCall, float]] = []
                results: dict[int, ToolResult] = {}
                for call_idx, call in enumerate(batch):
                    yield LoopEvent(
                        "tool_call_start",
                        {
                            "id": call.id,
                            "name": call.name,
                            "round": round_index,
                            "arguments": call.arguments or {},
                        },
                    )
                    duplicate = False
                    if self._config.tool_dedup:
                        sig = (call.name, _stable_json_hash(call.arguments or {}))
                        if sig in tool_call_signatures:
                            duplicate = True
                        else:
                            tool_call_signatures.add(sig)
                    if duplicate:
                        results[call_idx] = ToolResult(
                            success=False,
                            error="duplicate_call",
                            needsFollowup=True,
                            data={
                                "duplicate": True,
                                "message": _DUPLICATE_CALL_MESSAGE.format(name=call.name),
                            },
                        )
                    else:
                        pending.append((call_idx, call, time.monotonic()))

                # Phase 2: execute. Safe batches gather; barrier batches and
                # single calls run sequentially. Exceptions are collected,
                # not raised — they surface as tool_error events in call
                # order during phase 3.
                started_by_idx = {i: started for i, _, started in pending}
                errors: dict[int, str] = {}
                if pending:
                    if batch_safe and len(pending) > 1:
                        outcomes = await asyncio.gather(
                            *(self._execute_tool(call, ctx) for _, call, _ in pending),
                            return_exceptions=True,
                        )
                        for (call_idx, _, _), outcome in zip(pending, outcomes):
                            if isinstance(outcome, BaseException):
                                errors[call_idx] = str(outcome)
                            else:
                                results[call_idx] = outcome
                    else:
                        for call_idx, call, _ in pending:
                            try:
                                results[call_idx] = await self._execute_tool(call, ctx)
                            except Exception as exc:  # noqa: BLE001 — tool_error event
                                errors[call_idx] = str(exc)

                # Phase 3 (call order): counters, hook, result event,
                # transcript append, error breaker.
                for call_idx, call in enumerate(batch):
                    if call_idx in errors:
                        ctx.consecutive_tool_errors += 1
                        yield LoopEvent(
                            "tool_error",
                            {"id": call.id, "name": call.name, "error": errors[call_idx]},
                        )
                        result = ToolResult(
                            success=False, error=errors[call_idx], needsFollowup=True
                        )
                    else:
                        result = results[call_idx]
                        if result.success:
                            ctx.consecutive_tool_errors = 0
                            ctx.tool_successes += 1
                        else:
                            ctx.consecutive_tool_errors += 1

                    # ── hook: post_tool_result (spill / truncation) ──────
                    result = await self._hooks.post_tool_result(result, call, ctx)

                    ctx.tool_calls_used += 1
                    tool_started = started_by_idx.get(call_idx, time.monotonic())
                    duration_ms = int((time.monotonic() - tool_started) * 1000)
                    # Small preview for display/trace consumers (tool cards,
                    # logs); the full result only lives in the transcript
                    # (and spill storage when externalized).
                    preview = _result_content(result)
                    if len(preview) > 300:
                        preview = preview[:300] + "…"
                    yield LoopEvent(
                        "tool_call_result",
                        {
                            "id": call.id,
                            "name": call.name,
                            "success": result.success,
                            "durationMs": duration_ms,
                            "resultPreview": preview,
                            **(
                                {"result": _result_content(result)}
                                if self._config.persist_tool_results
                                else {}
                            ),
                            **({"error": result.error} if result.error else {}),
                        },
                    )
                    manager.append(
                        LLMMessage.text_of(
                            "tool",
                            _result_content(result),
                            name=call.name,
                            tool_call_id=call.id,
                        )
                    )

                    # consecutive-error breaker (runaway guard). In a parallel
                    # batch the remaining calls already ran — their results
                    # are recorded above; the breaker gates the NEXT batch.
                    if ctx.consecutive_tool_errors >= self._config.max_tool_errors:
                        decision = CompletionDecision(
                            status="failed",
                            reason="too many consecutive tool errors",
                            confidence=0.9,
                        )
                        narrate = await self._offer_narration(
                            content, round_index, had_tool_calls=True, ctx=ctx,
                            wrap_up=wrap_up, completion_redos=completion_redos,
                        )
                        if narrate is not None:
                            prompt, reason = narrate
                            completion_redos += 1
                            wrap_up = True
                            manager.append_fragment(NarrationRequest(prompt))
                            yield LoopEvent(
                                "hook_action",
                                {
                                    "hook": "before_completion",
                                    "action": "narrate",
                                    "reason": reason or "breaker narration",
                                    "round": round_index,
                                },
                            )
                            breaker_tripped = True
                            # The wrap-up round reuses this transcript — every
                            # tool_call must have a tool message or providers
                            # reject the request. Append real results for calls
                            # that already ran in this batch, synthetic skips
                            # for everything never executed.
                            remaining: list[tuple[ToolCall, int | None]] = [
                                (c, i)
                                for i, c in enumerate(batch)
                                if i > call_idx
                            ] + [
                                (c, None)
                                for _, later in batches[batch_idx + 1:]
                                for c in later
                            ]
                            for skipped, skip_idx in remaining:
                                if skip_idx is not None and skip_idx in errors:
                                    skip_content = f"Error: {errors[skip_idx]}"
                                elif skip_idx is not None and skip_idx in results:
                                    skip_content = _result_content(results[skip_idx])
                                else:
                                    skip_content = _BREAKER_SKIP_MESSAGE
                                manager.append(
                                    LLMMessage.text_of(
                                        "tool",
                                        skip_content,
                                        name=skipped.name,
                                        tool_call_id=skipped.id,
                                    ),
                                    kind=(
                                        "loop.breaker_skip"
                                        if skip_content is _BREAKER_SKIP_MESSAGE
                                        else "tool"
                                    ),
                                )
                            break  # leave the tool loop; wrap-up round runs next
                        record_terminal_content(content, tool_calls)
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
            # A narration grant broke out of the tool loop above — skip the
            # "executing" bookkeeping and run the wrap-up round directly.
            if wrap_up:
                continue
            yield LoopEvent(
                "stage_complete",
                {
                    "round": round_index,
                    "toolCallCount": len(tool_calls),
                    "consecutiveToolErrors": ctx.consecutive_tool_errors,
                    "elapsedMs": int((time.monotonic() - started) * 1000),
                },
            )
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
            round_index += 1

        _ = started  # reserved for a future durationMs stage_complete event


_SOFT_TIMEOUT_NOTICE = (
    "[system notice] The time budget for this task is exhausted. Do NOT call "
    "any more tools. Summarize what you have done so far and produce the "
    "final answer now."
)

#: Hard cap on before_completion-granted redos (discipline retries +
#: narration rounds) per run. Hooks bound themselves; this is the
#: defense-in-depth backstop so a faulty hook cannot spin the loop forever.
_MAX_COMPLETION_REDOS = 4

_DISCIPLINE_RETRY_NOTICE = (
    "[system notice] The previous reply described an intended action but did "
    "not actually issue any tool call. Either make the tool call now, or — "
    "if no tool is genuinely needed — give the final answer directly without "
    "open-ended phrasing."
)

_NARRATION_REQUEST = (
    "[system notice] The task ended without a natural-language summary. Do "
    "NOT call any tools. Summarize what was done and what the tool results "
    "showed, and give the user a clear final answer now."
)


class SoftTimeoutNotice(ContextFragment):
    """The soft-timeout wrap-up ask as a marked, self-recognisable fragment."""

    content_kind = "loop.soft_timeout_notice"

    def body(self) -> str:
        return _SOFT_TIMEOUT_NOTICE

    @classmethod
    def type_markers(cls) -> tuple[str, str]:
        return ("[system notice] The time budget", "")


class DisciplineRetryNotice(ContextFragment):
    """The before_completion discipline correction as a marked fragment."""

    content_kind = "loop.discipline_retry_notice"

    def __init__(self, message: str | None = None) -> None:
        self._message = message or _DISCIPLINE_RETRY_NOTICE

    def body(self) -> str:
        return self._message

    @classmethod
    def type_markers(cls) -> tuple[str, str]:
        return ("[system notice] The previous reply", "")


class NarrationRequest(ContextFragment):
    """The narration-round ask as a marked fragment (default or hook text)."""

    content_kind = "loop.narration_request"

    def __init__(self, message: str | None = None) -> None:
        self._message = message or _NARRATION_REQUEST

    def body(self) -> str:
        return self._message

    @classmethod
    def type_markers(cls) -> tuple[str, str]:
        return ("[system notice] The task ended", "")


_DUPLICATE_CALL_MESSAGE = (
    "You already called `{name}` with identical arguments in this turn; the "
    "result will not change. Continue from the existing tool result: use "
    "different arguments or a different tool, or reply to the user directly "
    "with your conclusion — do not repeat the same call."
)

_TOOL_TIMEOUT_MESSAGE = (
    "`{name}` produced no result within {timeout_ms}ms and was cancelled. Do "
    "not claim it succeeded; retry with different arguments, use a different "
    "tool, or continue without it."
)

#: Synthetic tool message appended for calls skipped when the consecutive-
#: error breaker trips but a narration round continues with the transcript
#: (every tool_call needs a tool message or providers reject the request).
_BREAKER_SKIP_MESSAGE = (
    "[not executed: the turn stopped after too many consecutive tool errors. "
    "Do not claim this call produced a result.]"
)


def _stable_json_hash(value: Any) -> str:
    """Hash JSON-serializable values for dedup correlation (ported from
    deeppath-api's ``tracing.stable_json_hash``)."""

    import hashlib
    import json

    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        payload = str(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


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
