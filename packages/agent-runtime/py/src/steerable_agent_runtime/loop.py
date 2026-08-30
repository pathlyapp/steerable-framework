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
    round boundaries; once exceeded the loop wraps up. Chat withholds tools
    and asks for a final answer; ``wrap_up_keeps_tools`` keeps offering
    tools for a few act rounds so Harbor can still score files on disk
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

from .errors import ApprovalAborted
from .history import (
    KIND_ASSISTANT,
    KIND_SYSTEM,
    KIND_USER,
    CompactionBoundary,
    ContextFragment,
    ContextManager,
    HistoryItem,
    HistorySeed,
    HistoryStore,
    entry_from_dict,
    entry_to_dict,
)
from .hooks import CompletionDraft, LoopHooks, NoopHooks
from .llm import LLMMessage, LLMProvider, LLMUsage
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

CompletionStatus = Literal[
    "executing", "completed", "failed", "budget_exhausted", "cancelled"
]


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
            # An upstream ApprovalExecutor bridges its allow verdict through
            # the context so the router's require_consent gate recognizes it
            # instead of double-gating.
            consent_granted=self._consent_granted or ctx.consent_granted,
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
    #: Prompt-cache accounting of the last completed request (zero when the
    #: provider reports none). Telemetry only — surfaced on stage_complete so
    #: cache stability is measurable; unlike the pressure indices above these
    #: describe one request, not a projection, so rewrites don't stale them.
    last_cached_prompt_tokens: int = 0
    last_cache_creation_tokens: int = 0
    #: Accumulated billable usage across every provider request this run
    #: (W6-9). Unlike the ``last_*`` pressure indices these SUM over requests —
    #: each request bills its own prompt+completion, so per-turn cost
    #: attribution needs the running totals, not the latest single request.
    accumulated_prompt_tokens: int = 0
    accumulated_completion_tokens: int = 0
    #: Set by an upstream ApprovalExecutor on its allow path; read by
    #: RouterToolExecutor to bridge the verdict into the router's
    #: require_consent gate. Plain False when no approval layer is wired.
    consent_granted: bool = False


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
    #: Wall-clock soft limit. When exceeded, the loop asks the model to wrap
    #: up instead of hard-killing the run. Default wrap-up withholds tools
    #: (one text round). ``None`` disables.
    soft_timeout_ms: int | None = None
    #: Coding evals (Harbor / Terminal-Bench) score files on disk, not chat
    #: text. Keep offering tools after the soft timeout so the model can
    #: still write those files. Chat stays False.
    wrap_up_keeps_tools: bool = False
    #: Extra act rounds after wrap-up when ``wrap_up_keeps_tools`` is set.
    #: Then tools are withheld for a final text round. Keep this small so
    #: Harbor's remaining wall clock can still kill the trial.
    wrap_up_max_tool_rounds: int = 4
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
    #: Bound on one mid-turn ``steer()`` injection (W4-7). Oversized
    #: injections are truncated with a visible marker. ``None`` disables
    #: (trusted hosts only).
    max_steer_chars: int | None = 32_000

    def __post_init__(self) -> None:
        if self.tool_timeout_ms is not None and self.tool_timeout_ms <= 0:
            raise ValueError("tool_timeout_ms must be positive (or None to disable)")
        if self.max_steer_chars is not None and self.max_steer_chars <= 0:
            raise ValueError("max_steer_chars must be positive (or None to disable)")
        if self.wrap_up_max_tool_rounds < 0:
            raise ValueError("wrap_up_max_tool_rounds must be >= 0")


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
        history_store: HistoryStore | None = None,
        record_id: str | None = None,
    ) -> None:
        self._provider = provider
        self._executor = executor
        self._config = config or LoopConfig()
        self._hooks: LoopHooks = hooks if hooks is not None else NoopHooks()
        # Durable record channel (Wave 1 step 5). When set, the loop flushes
        # the manager's pending entries before each LLM request, after each
        # tool batch, and at turn end — everything the model saw is durable
        # before the next request depends on it. ``record_id`` defaults to
        # the run's ``chat_id`` (the continuous per-chat log).
        self._history_store = history_store
        self._record_id = record_id
        # Mid-turn user messages land here via steer() and are drained into
        # the transcript at the next round boundary (dsh-style "inject":
        # consumed at the next step, no separate wakeup semantics).
        self._inbox: asyncio.Queue[str] = asyncio.Queue()
        # Cooperative cancellation token (see cancel()). Sticky for the loop
        # instance — a CoreLoop is single-run in practice (the sidecar builds
        # one per stream), so a cancel issued before run() still applies.
        self._cancel_event = asyncio.Event()
        # Compact trajectory recorded during run(); replayable via
        # replay.reduce_execution_state. Derived from the completion events
        # (single write path — see _emit_completion). Reset each run.
        self.trajectory: list[HarnessTrajectoryEvent] = []
        # The append-only model-visible record for the current run (the
        # transcript is its projection). Rebuilt per run() from the seed
        # messages; exposed for tests, persistence, and resume.
        self.history = ContextManager()
        # W6-9: the live LoopContext of the current/last run, so the host can
        # read accumulated billable usage after the run via `last_run_usage`.
        self._run_context: LoopContext | None = None

    def steer(self, content: str) -> None:
        """Inject a user message into a running turn.

        Called from the same event loop (e.g. a sidecar RPC handler) while
        ``run()`` is active; the message is appended to the transcript at the
        next round boundary and surfaced as a ``steer`` event. Messages sent
        after the run ends are ignored by the (already closed) consumer.

        Bounded (W4-7): an oversized injection is truncated to
        ``max_steer_chars`` with a visible marker rather than appended
        whole — a host bug must not be able to stuff an unbounded blob
        mid-turn.
        """
        if content:
            cap = self._config.max_steer_chars
            if cap is not None and len(content) > cap:
                content = (
                    f"{content[:cap]}\n…[steer message truncated at {cap} chars]"
                )
            self._inbox.put_nowait(content)

    def cancel(self) -> None:
        """Request cooperative cancellation of the current run.

        Called from the same event loop (e.g. a sidecar RPC handler) while
        ``run()`` is active. The loop winds down at the next safe point —
        round boundary, stream chunk, or tool-call slot — records the partial
        turn (streamed content as the terminal assistant message; real
        results for executed tool calls, a "cancelled" error for the
        in-flight one, synthetic skip notices for the rest, so the record
        never has dangling tool_calls), and emits a terminal completion with
        status ``"cancelled"``. An in-flight tool coroutine is
        asyncio-cancelled so a hung tool does not pin the turn. Sticky for
        the loop instance: a cancel issued before ``run()`` ends the run at
        the first round boundary.
        """
        self._cancel_event.set()

    @property
    def last_run_usage(self) -> LLMUsage | None:
        """Accumulated billable usage of the current/last run (W6-9).

        Summed over every provider request in the run — each request bills its
        own prompt+completion, so per-turn cost attribution needs these totals,
        not the latest single request. ``None`` before the first run.
        """
        ctx = self._run_context
        if ctx is None:
            return None
        return LLMUsage(
            prompt_tokens=ctx.accumulated_prompt_tokens,
            completion_tokens=ctx.accumulated_completion_tokens,
            total_tokens=ctx.accumulated_prompt_tokens + ctx.accumulated_completion_tokens,
            cached_prompt_tokens=ctx.last_cached_prompt_tokens,
            cache_creation_tokens=ctx.last_cache_creation_tokens,
        )

    async def _flush_history(
        self, manager: ContextManager, chat_id: str | None
    ) -> None:
        """Persist the record's new entries (no-op without a history store).

        ``record_id`` defaults to the run's ``chat_id`` — the continuous
        per-chat log (decision ② of the W1 design). Flush points: before
        each LLM request, after each tool batch, and at turn end, so a
        crash never loses more than the in-flight round.
        """
        if self._history_store is None:
            return
        record_id = self._record_id or chat_id
        if record_id is None:
            return
        pending = manager.drain_pending()
        if pending:
            await self._history_store.append_history(
                record_id, [entry_to_dict(entry) for entry in pending]
            )

    async def _plan_record_seeding(
        self, record_id: str, messages: list[LLMMessage]
    ) -> tuple[list[LLMMessage], int, int, dict[str, Any] | None]:
        """Plan how this run's seed joins the durable per-chat record.

        Returns ``(seed, first_seq, persisted_prefix, pre_boundary)``:

        - empty record → the host seed as-is, all of it flushes as new.
        - seed extends the durable projection exactly (projection-echoing
          hosts, tests) → seed as-is; only the tail past
          ``persisted_prefix`` flushes; seq continues the log.
        - seed reconciles with the record's host-visible view (production
          hosts rebuild a lossy per-turn view: final user/assistant texts
          only, assistant text display-transformed) → the run seeds from
          the RECORD's projection plus the host's new tail, so the model
          keeps the full history (tool rounds, injected fragments) and the
          record stays delta-only.
        - anything else (host edited/truncated history) → a declared
          ``host_revision`` boundary persists first, then the whole host
          seed flushes after it, keeping the durable projection coherent.
        """
        from .resume import load_history_items

        store = self._history_store
        assert store is not None  # caller guards on it
        latest = await store.list_history(record_id, limit=1, reverse=True)
        if not latest:
            return (messages, 0, 0, None)
        next_seq = int(latest[0].get("seq", 0)) + 1
        items = await load_history_items(store, record_id)
        if items:
            prior = [item.message for item in items]
            if list(prior) == messages[: len(prior)]:
                return (messages, next_seq, len(prior), None)
            new_tail = _reconcile_host_seed(items, messages)
            if new_tail is not None:
                return ([*prior, *new_tail], next_seq, len(prior), None)
            # W6-10: the direct reconcile failed. Before declaring a host
            # revision, see through the loop's OWN compactions — the host never
            # revised, it just reseeded the raw conversation while the record
            # holds a compacted projection. If the seed matches the
            # compaction-transparent host view, keep the compaction and append
            # only the genuinely-new tail (no double compression).
            see_through = await self._host_view_seeing_through_compactions(
                store, record_id
            )
            if see_through is not None:
                new_tail = _match_host_view(see_through, messages)
                if new_tail is not None:
                    return ([*prior, *new_tail], next_seq, len(prior), None)
        boundary = CompactionBoundary(
            seq=next_seq,
            reason="host revised history upstream of this run",
            action="host_revision",
        )
        return (messages, next_seq + 1, 0, entry_to_dict(boundary))

    async def _host_view_seeing_through_compactions(
        self, store: HistoryStore, record_id: str
    ) -> list[LLMMessage] | None:
        """The host-visible view with the loop's own compactions made transparent.

        Returns None when the record holds no loop compaction to see through —
        the caller then falls straight through to a ``host_revision`` exactly as
        before. Otherwise returns the reconstructed host view for the
        reconciliation fallback.
        """
        raw = await store.list_history(record_id)
        if not raw:
            return None
        entries = [entry_from_dict(r) for r in raw]
        if not any(
            isinstance(e, CompactionBoundary)
            and e.action in ("compact", "overflow_recovery")
            and e.replacement_count is not None
            for e in entries
        ):
            return None
        return _host_view_through_loop_compactions(entries)

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

    async def _execute_tool_cancellable(
        self, call: ToolCall, ctx: LoopContext
    ) -> tuple[ToolResult | None, bool]:
        """Race one tool call against cooperative cancellation.

        Returns ``(result, False)`` on normal completion. When the cancel
        token fires first, the in-flight coroutine is asyncio-cancelled (so a
        hung tool does not pin the turn) and ``(None, True)`` is returned —
        the caller records the call as cancelled and stops the batch.
        """
        exec_task = asyncio.ensure_future(self._execute_tool(call, ctx))
        cancel_waiter = asyncio.ensure_future(self._cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {exec_task, cancel_waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_waiter in done and not exec_task.done():
                exec_task.cancel()
                await asyncio.gather(exec_task, return_exceptions=True)
                return None, True
            return exec_task.result(), False
        finally:
            cancel_waiter.cancel()

    async def _gather_tools_cancellable(
        self, pending: list[tuple[int, ToolCall, float]], ctx: LoopContext
    ) -> tuple[list[ToolResult | BaseException], bool]:
        """Race a parallel batch against cooperative cancellation.

        Always returns per-call outcomes in pending order plus whether
        cancellation fired. On cancel, in-flight children are
        asyncio-cancelled; calls that already finished keep their real
        results, cancelled ones surface as ``asyncio.CancelledError``.
        """
        call_tasks = [
            asyncio.ensure_future(self._execute_tool(call, ctx))
            for _, call, _ in pending
        ]
        gather_task = asyncio.gather(*call_tasks, return_exceptions=True)
        cancel_waiter = asyncio.ensure_future(self._cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {gather_task, cancel_waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_waiter in done and not gather_task.done():
                gather_task.cancel()
                await asyncio.gather(gather_task, return_exceptions=True)
                return [
                    task.result()
                    if task.done() and not task.cancelled()
                    else asyncio.CancelledError("cancelled")
                    for task in call_tasks
                ], True
            return list(gather_task.result()), False
        finally:
            cancel_waiter.cancel()

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
        self._run_context = ctx
        # The transcript is a projection of the append-only record; nothing
        # mutates a list in place anymore (see history.py).
        record_id = self._record_id or chat_id
        seed = list(messages)
        first_seq = 0
        persisted_prefix = 0
        pre_boundary: dict[str, Any] | None = None
        if self._history_store is not None and record_id is not None:
            seed, first_seq, persisted_prefix, pre_boundary = (
                await self._plan_record_seeding(record_id, list(messages))
            )
        manager = ContextManager(
            seed, token_model=self._provider.model, first_seq=first_seq
        )
        if persisted_prefix:
            manager.mark_persisted_prefix(persisted_prefix)
        if (
            pre_boundary is not None
            and self._history_store is not None
            and record_id is not None
        ):
            # The host revised history upstream of this run — declare the
            # rewrite in the durable log before the fresh seed lands, so the
            # record's projection stays coherent (append-only, auditable).
            await self._history_store.append_history(record_id, [pre_boundary])
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
        wrap_up_tool_rounds_used = 0

        def withholding_tools() -> bool:
            """True when this round must not offer or execute tools."""
            if not wrap_up:
                return False
            if not self._config.wrap_up_keeps_tools:
                return True
            return wrap_up_tool_rounds_used >= self._config.wrap_up_max_tool_rounds

        self.trajectory = []

        def emit_completion(
            step: dict[str, Any], dec: CompletionDecision
        ) -> LoopEvent:
            """Build the completion event carrying its full step summary, and
            derive the trajectory entry from it (single write path — the
            trajectory is never recorded through a separate channel, so the
            event stream alone can rebuild it)."""
            data = {**step, **_decision_data(dec)}
            # W6-9: running billable usage rides the event (NOT the trajectory
            # step — the trajectory keeps the stable api/agent-aligned shape).
            # The terminal completion carries the run's final totals.
            data["usage"] = {
                "promptTokens": ctx.accumulated_prompt_tokens,
                "completionTokens": ctx.accumulated_completion_tokens,
                "totalTokens": (
                    ctx.accumulated_prompt_tokens + ctx.accumulated_completion_tokens
                ),
                "cachedPromptTokens": ctx.last_cached_prompt_tokens,
            }
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
            manager.append(_assistant_message(content))

        def _assistant_message(
            content: str, tool_calls: list[ToolCall] | None = None
        ) -> LLMMessage:
            return LLMMessage.text_of(
                "assistant",
                content,
                tool_calls=tool_calls,
                reasoning="".join(reasoning_parts) or None,
                reasoning_details=list(reasoning_details_acc) or None,
            )

        yield LoopEvent("stage_start", {"model": self._provider.model})

        decision = CompletionDecision(status="failed", reason="loop did not run")
        round_index = 0
        reasoning_parts: list[str] = []
        reasoning_details_acc: list[Any] = []
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

            # ── cooperative cancel: round boundary ───────────────────────
            # Checked after the steer drain so an accepted injection is
            # recorded even when the turn is ending. Nothing from this round
            # has reached the transcript yet, so there is nothing to rewind.
            if self._cancel_event.is_set():
                await self._flush_history(manager, chat_id)
                yield emit_completion(
                    step_summary(
                        round_index=round_index,
                        finish_reason="cancelled",
                        content="",
                        tool_calls=[],
                    ),
                    CompletionDecision(
                        status="cancelled",
                        reason="cancelled by host request",
                        confidence=1.0,
                    ),
                )
                return

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
                manager.append_fragment(
                    SoftTimeoutNotice(
                        body=(
                            _SOFT_TIMEOUT_NOTICE_KEEP_TOOLS
                            if self._config.wrap_up_keeps_tools
                            else _SOFT_TIMEOUT_NOTICE
                        )
                    )
                )

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
                    if item.fragment is not None:
                        # Fragment-carrying appends go through the bounded
                        # path so an over-cap injection degrades predictably.
                        manager.append_fragment(item.fragment)
                    else:
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
                pre.tool_choice
                if (pre.tool_choice and tools and not withholding_tools())
                else None
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
            reasoning_parts.clear()
            reasoning_details_acc.clear()
            # Display pipeline: raw text is accumulated for recovery and the
            # transcript, but content_delta events pass through a surrogate
            # carry (half-emoji split across chunks) and the pseudo stripper
            # (echo blocks / pseudo calls never reach the user's screen).
            stripper = PseudoStreamStripper()
            content_carry = ""
            reasoning_carry = ""
            stream_cancelled = False
            while True:
                try:
                    # Everything the model is about to see is durable first
                    # (also covers the overflow-recovery rewrite on retries).
                    await self._flush_history(manager, chat_id)
                    stream = self._provider.stream(
                        manager.projection,
                        # wrap-up round: withhold tool descriptors so the model
                        # cannot start another act phase.
                        tools=None if withholding_tools() else tools,
                        temperature=self._config.temperature,
                        max_tokens=self._config.max_tokens,
                        **(
                            {"tool_choice": step_tool_choice}
                            if step_tool_choice
                            else {}
                        ),
                    )
                    async for chunk in stream:
                        # Cooperative cancel: stop consuming at the next
                        # chunk rather than mid-processing one.
                        if self._cancel_event.is_set():
                            stream_cancelled = True
                            break
                        if chunk.content_delta:
                            content_parts.append(chunk.content_delta)
                            emit, content_carry = split_trailing_high_surrogate(
                                chunk.content_delta, content_carry
                            )
                            display = stripper.feed(emit)
                            if display:
                                yield LoopEvent("content_delta", {"delta": display})
                        if chunk.reasoning_delta:
                            reasoning_parts.append(chunk.reasoning_delta)
                            emit, reasoning_carry = split_trailing_high_surrogate(
                                chunk.reasoning_delta, reasoning_carry
                            )
                            if emit:
                                yield LoopEvent("reasoning_delta", {"delta": emit})
                        if chunk.reasoning_details:
                            reasoning_details_acc.extend(chunk.reasoning_details)
                        if chunk.tool_call_delta is not None:
                            tool_calls.append(chunk.tool_call_delta)
                        if chunk.usage is not None:
                            # Ground-truth context size for compaction pressure.
                            ctx.last_prompt_tokens = chunk.usage.prompt_tokens
                            ctx.last_prompt_transcript_len = len(manager.projection)
                            ctx.last_cached_prompt_tokens = chunk.usage.cached_prompt_tokens
                            ctx.last_cache_creation_tokens = (
                                chunk.usage.cache_creation_tokens
                            )
                            # W6-9: running billable totals for per-turn cost
                            # attribution (summed over every request).
                            ctx.accumulated_prompt_tokens += chunk.usage.prompt_tokens
                            ctx.accumulated_completion_tokens += (
                                chunk.usage.completion_tokens
                            )
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
                                await self._flush_history(manager, chat_id)
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
                    break  # stream completed (or cancelled) without error
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
                    await self._flush_history(manager, chat_id)
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

            if stream_cancelled:
                # Cancelled mid-stream: keep the partial content as the
                # terminal assistant message. Tool calls the model streamed
                # but never executed are dropped from the record — recording
                # them would leave dangling tool_calls — but stay on the
                # completion event's step summary for the trace.
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    await aclose()
                record_terminal_content("".join(content_parts), [])
                await self._flush_history(manager, chat_id)
                yield emit_completion(
                    step_summary(
                        round_index=round_index,
                        finish_reason="cancelled",
                        content="".join(content_parts),
                        tool_calls=tool_calls,
                    ),
                    CompletionDecision(
                        status="cancelled",
                        reason="cancelled by host request",
                        confidence=1.0,
                    ),
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
            # Skipped when wrap-up is withholding tools: any tool intent
            # (structured or pseudo) is dropped so the turn ends.
            if withholding_tools():
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
                            manager.append(_assistant_message(content))
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
                await self._flush_history(manager, chat_id)
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
            manager.append(_assistant_message(content, tool_calls))

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

                # Cooperative cancel between batches: nothing in this batch
                # has started, so every remaining call gets a skip notice.
                if self._cancel_event.is_set():
                    _append_unexecuted_tool_results(
                        manager,
                        batch=batch,
                        batch_idx=batch_idx,
                        batches=batches,
                        call_idx=-1,
                        errors={},
                        results={},
                        skip_message=_CANCEL_SKIP_MESSAGE,
                        skip_kind="loop.cancel_skip",
                    )
                    record_terminal_content(content, tool_calls)
                    await self._flush_history(manager, chat_id)
                    yield emit_completion(
                        step_summary(
                            round_index=round_index,
                            finish_reason="tool_calls",
                            content=content,
                            tool_calls=tool_calls,
                        ),
                        CompletionDecision(
                            status="cancelled",
                            reason="cancelled by host request",
                            confidence=1.0,
                        ),
                    )
                    return

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
                # An ApprovalAborted surfacing from any call ends the turn
                # after this batch's bookkeeping (see phase 3) — distinct
                # from a denial, which is an ordinary failed ToolResult.
                abort_exc: ApprovalAborted | None = None
                abort_idx: int | None = None
                # Cooperative cancel landing mid-batch mirrors the abort
                # path: the in-flight call is recorded as cancelled, calls
                # never started get synthetic skip notices (phase 3), and the
                # turn ends with status "cancelled".
                cancel_idx: int | None = None
                if pending:
                    if batch_safe and len(pending) > 1:
                        outcomes, cancel_hit = await self._gather_tools_cancellable(
                            pending, ctx
                        )
                        for (call_idx, _, _), outcome in zip(pending, outcomes):
                            if isinstance(outcome, ApprovalAborted):
                                errors[call_idx] = str(outcome)
                                if abort_exc is None:
                                    abort_exc = outcome
                                    abort_idx = call_idx
                            elif isinstance(outcome, asyncio.CancelledError):
                                errors[call_idx] = "cancelled"
                            elif isinstance(outcome, BaseException):
                                errors[call_idx] = str(outcome)
                            else:
                                results[call_idx] = outcome
                        if cancel_hit:
                            # Every call in this batch has an outcome (real or
                            # cancelled); phase 3 records them all, then the
                            # cancel finalize at the batch's last call covers
                            # later batches with skip notices.
                            cancel_idx = len(batch) - 1
                    else:
                        for call_idx, call, _ in pending:
                            try:
                                result, cancel_hit = (
                                    await self._execute_tool_cancellable(call, ctx)
                                )
                            except ApprovalAborted as exc:
                                errors[call_idx] = str(exc)
                                abort_exc = exc
                                abort_idx = call_idx
                                # Abort is a stop signal: nothing further in
                                # this batch (or turn) executes.
                                break
                            except Exception as exc:  # noqa: BLE001 — tool_error event
                                errors[call_idx] = str(exc)
                                continue
                            if cancel_hit:
                                errors[call_idx] = "cancelled"
                                cancel_idx = call_idx
                                break
                            results[call_idx] = result

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
                            # W4-2: lift the per-exec sandbox marker out of
                            # result.data so stream consumers (tool cards)
                            # can show the enforcement actually applied
                            # without parsing the result body.
                            **(
                                {"sandbox": result.data["_sandbox"]}
                                if isinstance(result.data, dict)
                                and isinstance(result.data.get("_sandbox"), dict)
                                else {}
                            ),
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

                    # Approval abort ends the turn: record the batch like the
                    # breaker does (real results for executed calls, synthetic
                    # skips for the rest — no dangling tool_calls) and finish
                    # as failed. No narration offer: stopping is the
                    # approver's explicit intent.
                    if call_idx == abort_idx:
                        _append_unexecuted_tool_results(
                            manager,
                            batch=batch,
                            batch_idx=batch_idx,
                            batches=batches,
                            call_idx=call_idx,
                            errors=errors,
                            results=results,
                            skip_message=_APPROVAL_ABORT_SKIP_MESSAGE,
                            skip_kind="loop.abort_skip",
                        )
                        record_terminal_content(content, tool_calls)
                        await self._flush_history(manager, chat_id)
                        yield emit_completion(
                            step_summary(
                                round_index=round_index,
                                finish_reason="tool_calls",
                                content=content,
                                tool_calls=tool_calls,
                            ),
                            CompletionDecision(
                                status="failed",
                                reason=f"approval aborted: {abort_exc}",
                                confidence=1.0,
                            ),
                        )
                        return

                    # Cooperative cancel ends the turn: the batch's calls are
                    # recorded above (real results, or "cancelled" for the
                    # in-flight one); everything never started gets a skip
                    # notice — no dangling tool_calls.
                    if call_idx == cancel_idx:
                        _append_unexecuted_tool_results(
                            manager,
                            batch=batch,
                            batch_idx=batch_idx,
                            batches=batches,
                            call_idx=call_idx,
                            errors=errors,
                            results=results,
                            skip_message=_CANCEL_SKIP_MESSAGE,
                            skip_kind="loop.cancel_skip",
                        )
                        record_terminal_content(content, tool_calls)
                        await self._flush_history(manager, chat_id)
                        yield emit_completion(
                            step_summary(
                                round_index=round_index,
                                finish_reason="tool_calls",
                                content=content,
                                tool_calls=tool_calls,
                            ),
                            CompletionDecision(
                                status="cancelled",
                                reason="cancelled by host request",
                                confidence=1.0,
                            ),
                        )
                        return

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
                            _append_unexecuted_tool_results(
                                manager,
                                batch=batch,
                                batch_idx=batch_idx,
                                batches=batches,
                                call_idx=call_idx,
                                errors=errors,
                                results=results,
                                skip_message=_BREAKER_SKIP_MESSAGE,
                                skip_kind="loop.breaker_skip",
                            )
                            break  # leave the tool loop; wrap-up round runs next
                        record_terminal_content(content, tool_calls)
                        await self._flush_history(manager, chat_id)
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
            # A text wrap-up (tools withheld) broke out of the tool loop
            # above — skip the "executing" bookkeeping and run the wrap-up
            # round directly. Artifact wrap-up still executes tools, so it
            # falls through and increments like a normal round.
            if wrap_up and withholding_tools():
                continue
            if wrap_up:
                wrap_up_tool_rounds_used += 1
            # Tool results are durable before the next round builds on them.
            await self._flush_history(manager, chat_id)
            yield LoopEvent(
                "stage_complete",
                {
                    "round": round_index,
                    "toolCallCount": len(tool_calls),
                    "consecutiveToolErrors": ctx.consecutive_tool_errors,
                    "elapsedMs": int((time.monotonic() - started) * 1000),
                    # Cache telemetry (Wave 2): the hit ratio
                    # cachedPromptTokens / promptTokens is the observable
                    # world-state diffing (next Wave-2 item) has to move.
                    "promptTokens": ctx.last_prompt_tokens or 0,
                    "cachedPromptTokens": ctx.last_cached_prompt_tokens,
                    "cacheCreationTokens": ctx.last_cache_creation_tokens,
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

_SOFT_TIMEOUT_NOTICE_KEEP_TOOLS = (
    "[system notice] The time budget for this task is nearly exhausted. "
    "Write the required output files now with bash, write_file, or edit_file. "
    "Hidden tests score those files, not this chat. Do not keep exploring."
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

    def __init__(self, body: str | None = None) -> None:
        self._body = body or _SOFT_TIMEOUT_NOTICE

    def body(self) -> str:
        return self._body

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

_APPROVAL_ABORT_SKIP_MESSAGE = (
    "[not executed: the turn was stopped by an approval abort. "
    "Do not claim this call produced a result.]"
)

#: Synthetic tool message for calls that never ran because the turn was
#: cancelled (same no-dangling-tool_calls invariant as the breaker/abort).
_CANCEL_SKIP_MESSAGE = (
    "[not executed: the turn was cancelled before this call ran. "
    "Do not claim this call produced a result.]"
)


def _append_unexecuted_tool_results(
    manager: ContextManager,
    *,
    batch: list[ToolCall],
    batch_idx: int,
    batches: list[tuple[bool, list[ToolCall]]],
    call_idx: int,
    errors: dict[int, str],
    results: dict[int, ToolResult],
    skip_message: str,
    skip_kind: str,
) -> None:
    """Append tool messages for calls that never executed.

    The assistant message carrying these tool_calls is already in the
    transcript; providers reject requests with dangling tool_calls, so every
    call gains a response — real content for calls that ran, a synthetic skip
    notice for the rest.
    """
    remaining: list[tuple[ToolCall, int | None]] = [
        (c, i) for i, c in enumerate(batch) if i > call_idx
    ] + [(c, None) for _, later in batches[batch_idx + 1 :] for c in later]
    for skipped, skip_idx in remaining:
        if skip_idx is not None and skip_idx in errors:
            skip_content = f"Error: {errors[skip_idx]}"
        elif skip_idx is not None and skip_idx in results:
            skip_content = _result_content(results[skip_idx])
        else:
            skip_content = skip_message
        manager.append(
            LLMMessage.text_of(
                "tool",
                skip_content,
                name=skipped.name,
                tool_call_id=skipped.id,
            ),
            kind=(skip_kind if skip_content is skip_message else "tool"),
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


def _is_host_visible(item: HistoryItem) -> bool:
    """The host-visible kinds: bare system/user plus terminal assistant
    messages (tool-call rounds are loop-internal)."""
    return (
        item.kind in (KIND_SYSTEM, KIND_USER)
        or (item.kind == KIND_ASSISTANT and not item.message.tool_calls)
    )


def _host_view_through_loop_compactions(
    entries: list[HistoryItem | CompactionBoundary | HistorySeed],
) -> list[LLMMessage]:
    """The host-visible conversation, seeing through the loop's OWN compactions.

    A ``compact`` / ``overflow_recovery`` boundary is the loop compressing its
    own record — the host never revised anything and still holds the raw
    pre-compaction conversation. The rewrite that follows such a boundary
    (head + summary marker + tail, ``replacement_count`` messages) re-states
    content the host already has, so it is skipped: the view stays the host's
    full conversation and the next raw seed reconciles instead of declaring a
    spurious ``host_revision`` that would discard the compaction (W6-10).

    A ``host_revision`` boundary is a genuine host edit — the pre-boundary
    span is dropped (the view resets), matching the post-boundary projection.
    Boundaries without a ``replacement_count`` (older records) are opaque and
    reset the view the same way, preserving the pre-W6-10 behavior.
    """
    view: list[LLMMessage] = []
    i = 0
    n = len(entries)
    while i < n:
        entry = entries[i]
        if isinstance(entry, CompactionBoundary):
            if (
                entry.action in ("compact", "overflow_recovery")
                and entry.replacement_count is not None
            ):
                # See through the loop's own compaction: skip the rewrite.
                i += 1 + entry.replacement_count
                continue
            # host_revision or an opaque (legacy) boundary: the visible span
            # restarts here.
            view = []
            i += 1
            continue
        if isinstance(entry, HistorySeed):
            for message in entry.messages:
                if message.role in ("system", "user") or (
                    message.role == "assistant" and not message.tool_calls
                ):
                    view.append(message)
        elif _is_host_visible(entry):
            view.append(entry.message)
        i += 1
    return view


def _reconcile_host_seed(
    items: list[HistoryItem], seed: list[LLMMessage]
) -> list[LLMMessage] | None:
    """Reconcile a production host's per-turn seed against the record.

    Hosts rebuild history from their own store each turn: final
    user/assistant texts only (no tool rounds, no loop-injected
    fragments), with assistant text display-transformed (trimmed, host
    sections appended). The seed reconciles when its history part matches
    the record's host-visible view — bare ``system``/``user`` kinds plus
    terminal assistant messages (tool-call rounds are loop-internal) —
    comparing user/system text exactly and assistant text up to a
    host-appended suffix. Returns the seed's new tail on match; None means
    the history genuinely changed (edit/truncate/regenerate) and the
    caller declares a ``host_revision`` boundary.
    """
    host_view = [item.message for item in items if _is_host_visible(item)]
    return _match_host_view(host_view, seed)


def _match_host_view(
    host_view: list[LLMMessage], seed: list[LLMMessage]
) -> list[LLMMessage] | None:
    """Match a host seed's history part against a host-visible view.

    The seed reconciles when its leading ``len(host_view)`` messages match the
    view — user/system text exactly, assistant text up to a host-appended
    suffix. Returns the seed's new tail on match; None on a genuine divergence.
    """
    if len(seed) <= len(host_view):
        return None
    history_part, new_tail = seed[: len(host_view)], seed[len(host_view) :]
    for expected, actual in zip(host_view, history_part):
        if expected.role != actual.role:
            return None
        want = expected.content_text.strip()
        got = actual.content_text.strip()
        if expected.role == "assistant":
            if got != want and not (want and got.startswith(want)):
                return None
        elif got != want:
            return None
    return list(new_tail)


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
