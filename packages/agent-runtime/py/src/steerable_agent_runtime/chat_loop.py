"""ChatLoop — the framework's canonical Think-Act-Observe loop.

See ``spec/runtime/chat-loop.md`` for the design contract:

* 8 core responsibilities (round scheduling, LLM stream, tool dispatch, …)
* 11 hook points (callback registry)
* Standard SSE event sequence
* Lifecycle state machine

This file is implemented in 5 incremental slices (A1.1 — A1.5):

* **A1.1** — Skeleton: ``ChatLoop``, ``LoopConfig``, ``HookContext`` hierarchy,
  ``HOOK_SKIP``, hook registry, ``HookError``, session envelope, and the
  ``loop_start`` / ``loop_end`` hooks.
* **A1.2** — Round body: ``provider.stream`` orchestration,
  ``tool_call_delta`` accumulation, ``ToolRouter.dispatch``, tool-result
  re-feed, ``max_rounds`` hard cap. 6 additional hooks live.
* **A1.3** — Harness integration: ``LoopConfig.budget``
  (``BudgetLimit``) drives per-round ``consume_budget`` updates;
  ``decide_completion`` runs at the end of every round and its dict form
  populates ``RoundEndCtx.decision``. The natural-stop branch from A1.2 is
  replaced by ``decision.status != "executing"``; ``max_rounds`` survives as
  a top-level safety net reported with ``limit_kind="rounds"``.
* **A1.4** — Remaining 3 hooks (``emit`` rewriting,
  ``budget_exhausted``, ``error``) + ``asyncio.CancelledError`` path
  (``final_status="cancelled"``). All ``SSEEvent`` emissions are funneled
  through ``_emit()`` so callers can rewrite, suppress (``HOOK_SKIP``), or
  split events without re-implementing the loop. New event subtypes:
  ``agent.event=budget_exhausted``, ``agent.event=error``.
* **A1.5a** — Loop-owned safety nets:
  ``max_tool_result_bytes`` truncation of LLM-visible tool content (the
  ``ToolResult`` and ``after_tool_result`` ctx remain intact);
  ``max_elapsed_seconds`` wall-clock guard at each round entry, reported
  as ``limit_kind="time"`` via ``budget_exhausted``.
* **A1.5b** — LLM-stream retry via ``LoopConfig.retry_policy``
  (a ``steerable_agent_harness.RetryPolicy``). ``_stream_with_retry``
  wraps ``provider.stream`` so retry covers **stream creation + first
  chunk only**; mid-stream exceptions propagate verbatim (retrying after
  any chunk yielded would corrupt accumulators). Retries are silent
  w.r.t. the ``error`` hook — only the final failure surfaces, and
  ``CancelledError`` / ``HookError`` always bypass retry. The default
  classifier (``harness.is_retryable_error``) covers asyncio/network/IO
  errors plus the ``should_retry`` attribute opt-in.
* **A1.5c** (this slice) — ``HarnessTrace`` persistence via the
  ``StorageAdapter`` injected on ``LoopConfig``. ``_TraceRecorder``
  buffers spans (``loop`` / ``round`` / ``llm`` / ``tool``) and
  events (``loop.start`` / ``round.{start,end}`` / ``loop.end`` /
  ``error`` / ``budget.exhausted`` / ``loop.cancelled``) and flushes
  per round + at finalisation. ``storage=None`` is a strict no-op —
  zero extra ``await``s, identical externally to A1.5b. Storage
  failures are best-effort: logged at ``WARNING`` and disable further
  trace writes for the rest of the run, never propagated to callers.
* **A1.5d.1** (this slice) — OpenAI streaming **partial-args
  reassembly**. ``function.arguments`` arrives as JSON string fragments
  across many SSE chunks; the previous per-chunk ``json.loads`` discarded
  every fragment as ``{}``. ``OpenAICompatProvider.stream()`` now keeps
  an internal ``tool_buf`` keyed by ``tool_calls[].index`` and emits
  the **monotonically non-shrinking** best-effort parse on every delta.
  ChatLoop's accumulator (which overwrites ``arguments`` per delta)
  thus converges to the full dict by ``finish_reason="tool_calls"``.
* **A1.5d.2** — Anthropic ``input_json_delta`` parity (same bug,
  different protocol), deferred until a PoC actually exercises the
  Anthropic native path.

The public surface (``ChatLoop``, ``LoopConfig``, the ``HookContext`` subclasses,
``HOOK_SKIP``, ``HookError``) is stable from A1.1 onwards — subsequent slices
fill in behaviour, not interface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from steerable_agent_harness import (
    BudgetLimit,
    BudgetState,
    CompletionDecision,
    RetryPolicy,
    consume_budget,
    decide_completion,
    is_retryable_error,
    next_retry_delay_ms,
)
from steerable_agent_protocol.generated import (
    HarnessTrace,
    SSEEvent,
    ToolCall,
    ToolResult,
    TraceEvent,
    TraceSpan,
)

from .llm import LLMMessage, LLMProvider, LLMStreamChunk, LLMUsage
from .storage import StorageAdapter
from .tools import ToolRouter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public sentinels & literals
# ---------------------------------------------------------------------------


class _HookSkipSentinel:
    """Singleton sentinel; use ``HOOK_SKIP`` rather than instantiating directly.

    Returned from a hook callback to short-circuit the loop's default behaviour
    at that hook site. Only legal for the hooks documented as allowing skip
    (see ``spec/runtime/chat-loop.md`` §5.2): ``before_round``, ``emit``,
    ``before_tool_call``. Returning ``HOOK_SKIP`` from any other hook is a
    programming error; the loop will raise.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<HOOK_SKIP>"


HOOK_SKIP = _HookSkipSentinel()


HookName = Literal[
    "loop_start",
    "loop_end",
    "before_round",
    "after_round",
    "before_send_messages",
    "after_assistant_message",
    "before_tool_call",
    "after_tool_result",
    "emit",
    "budget_exhausted",
    "error",
]


CompletionStatus = Literal["completed", "failed", "budget_exhausted", "cancelled"]


ProviderKind = Literal["openai_compat", "anthropic_native"]


# ---------------------------------------------------------------------------
# LoopConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoopConfig:
    """Caller-supplied loop configuration. Read-only from inside the loop.

    A snapshot of this object is exposed on every ``HookContext.config`` so
    hook callbacks can read it without keeping their own reference.
    """

    provider: LLMProvider
    provider_kind: ProviderKind
    tool_router: ToolRouter
    storage: StorageAdapter | None = None
    # Override the auto-generated session id (must start with ``sess_``).
    session_id: str | None = None
    initial_messages: Sequence[LLMMessage] = field(default_factory=tuple)
    # Hook state seed; copied into a mutable per-run dict before the first hook.
    initial_state: dict[str, Any] = field(default_factory=dict)
    # Top-level safety net on round count. Always honoured. When ``budget`` is
    # also set the loop respects whichever cap trips first (rounds vs.
    # ``budget.max_steps``).
    max_rounds: int = 12
    max_elapsed_seconds: float = 180.0
    # Each ``ToolResult.value`` larger than this is truncated before re-feed.
    max_tool_result_bytes: int = 64 * 1024
    # Harness budget — tokens / steps / tool_calls. When None the loop only
    # enforces ``max_rounds``. Carried into every ``decide_completion`` call.
    budget: BudgetLimit | None = None
    # Retry policy is plumbed onto every hook ctx but not consumed by the
    # loop yet; A1.4 wraps LLM calls and A1.5 wraps tool dispatch in retries.
    retry_policy: RetryPolicy | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    # If None, the loop calls ``tool_router.describe()`` to obtain the
    # OpenAI-function-shape tool descriptors sent to the model.
    tool_descriptors: list[dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# HookContext hierarchy (mirrors spec/runtime/chat-loop.md §7)
# ---------------------------------------------------------------------------
#
# Design notes:
# * Every subclass carries the shared ``HookContext`` fields plus zero or more
#   hook-specific fields.
# * ``state`` is a ``MutableMapping`` and is the **only** sanctioned channel
#   for hook callbacks to share data across hook fires. The same dict is
#   threaded through every ctx for one ``run()`` call.
# * Per-hook ctx subclasses for hooks that A1.1 doesn't fire yet are defined
#   here too, so the public surface is stable. A1.2 — A1.5 just start firing
#   them with real payloads.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HookContext:
    loop_id: str
    session_id: str
    trace_id: str
    config: LoopConfig
    state: MutableMapping[str, Any]
    storage: StorageAdapter | None


@dataclass(slots=True)
class LoopStartCtx(HookContext):
    initial_messages: list[LLMMessage] = field(default_factory=list)
    initial_tools: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class LoopEndCtx(HookContext):
    # ``final_status`` is always set; ``final_decision`` is populated from A1.3
    # onwards (when ``decide_completion`` runs) and carries the full
    # ``CompletionDecision`` payload as a dict. The two coexist so callers that
    # only need a coarse-grained status don't have to unpack the decision.
    final_status: CompletionStatus = "completed"
    final_decision: dict[str, Any] | None = None
    rounds_completed: int = 0
    total_usage: dict[str, Any] | None = None


# Hook ctxs below are defined for public-surface stability but are not yet
# fired by the loop. A1.2 — A1.5 wire them up.


@dataclass(slots=True)
class RoundStartCtx(HookContext):
    round_index: int = 0
    # In-place editable: ``ctx.messages.insert(...)`` and
    # ``ctx.tools.append(...)`` both propagate to the round's working buffer.
    messages: list[LLMMessage] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class RoundEndCtx(HookContext):
    round_index: int = 0
    assistant_message: LLMMessage | None = None
    tool_calls: list[Any] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    # ``decision`` is the dict form of ``CompletionDecision`` (filled in A1.3).
    decision: dict[str, Any] | None = None
    # ``finish_reason`` is the LLM's stop reason for this round
    # (e.g. ``"stop"``, ``"tool_calls"``, ``"length"``). Useful for hooks that
    # want to react to length-cutoffs without re-tokenising.
    finish_reason: str | None = None


@dataclass(slots=True)
class SendMessagesCtx(HookContext):
    messages: list[LLMMessage] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    provider_kind: ProviderKind = "openai_compat"
    temperature: float | None = None
    max_tokens: int | None = None


@dataclass(slots=True)
class AssistantMessageCtx(HookContext):
    message: LLMMessage | None = None
    # Accumulated reasoning text from this round's ``reasoning_delta`` chunks.
    # Separate from ``message.content`` because the providers report it on a
    # distinct channel and most downstream parsers display it differently.
    reasoning: str | None = None
    usage: dict[str, Any] | None = None


@dataclass(slots=True)
class ToolCallCtx(HookContext):
    tool_call: Any = None  # protocol.generated.ToolCall — typed in A1.2
    round_index: int = 0


@dataclass(slots=True)
class ToolResultCtx(HookContext):
    tool_call: Any = None
    tool_result: Any = None  # protocol.generated.ToolResult — typed in A1.2
    round_index: int = 0


@dataclass(slots=True)
class EmitCtx(HookContext):
    event: SSEEvent | None = None


@dataclass(slots=True)
class BudgetExhaustedCtx(HookContext):
    limit_kind: str = ""
    budget_state: dict[str, Any] | None = None


@dataclass(slots=True)
class ErrorCtx(HookContext):
    exception: BaseException | None = None
    phase: Literal["llm_stream", "tool_dispatch", "hook"] = "llm_stream"
    round_index: int = 0


# ---------------------------------------------------------------------------
# Hook registry
# ---------------------------------------------------------------------------


HookCallback = Callable[[HookContext], Awaitable[Any]]


_ALL_HOOK_NAMES: frozenset[str] = frozenset(
    [
        "loop_start",
        "loop_end",
        "before_round",
        "after_round",
        "before_send_messages",
        "after_assistant_message",
        "before_tool_call",
        "after_tool_result",
        "emit",
        "budget_exhausted",
        "error",
    ]
)


_SKIP_ALLOWED: frozenset[str] = frozenset(
    ["before_round", "emit", "before_tool_call"]
)


class HookError(RuntimeError):
    """Wraps any exception raised by a hook callback.

    Re-raised from inside ``run()`` so callers see ``HookError(name=..., cause=...)``
    rather than a bare hook exception. This makes it possible to distinguish
    "the LLM broke" from "the hook code broke" without inspecting tracebacks.
    """

    def __init__(self, *, name: str, cause: BaseException) -> None:
        super().__init__(f"hook {name!r} raised: {cause!r}")
        self.name = name
        self.cause = cause


class _HookRegistry:
    """Callback registry, one list per hook name.

    Multiple callbacks per hook are allowed and run in registration order.
    Each callback may return:

    * ``None`` — no change, continue
    * ``HOOK_SKIP`` — skip the default behaviour at this hook site (only legal
      for hooks in ``_SKIP_ALLOWED``)
    * any other value — passed through as the "current" value for the next
      callback in the chain, and ultimately returned from ``fire()``

    The "value threading" semantics make ``before_send_messages``,
    ``after_tool_result``, etc. compose naturally: each callback receives the
    edits made by the previous one.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, list[HookCallback]] = {}

    def register(self, name: str, fn: HookCallback) -> None:
        if name not in _ALL_HOOK_NAMES:
            raise ValueError(
                f"unknown hook name: {name!r} "
                f"(known: {sorted(_ALL_HOOK_NAMES)})"
            )
        self._by_name.setdefault(name, []).append(fn)

    def has(self, name: str) -> bool:
        return bool(self._by_name.get(name))

    async def fire(self, name: str, ctx: HookContext) -> Any:
        """Run all callbacks for ``name``.

        Returns the last non-None value any callback returned, or None.
        Raises ``HookError`` if a callback raises.
        Raises ``RuntimeError`` if a callback returns ``HOOK_SKIP`` for a hook
        that does not permit skipping.
        """
        callbacks = self._by_name.get(name)
        if not callbacks:
            return None
        last: Any = None
        for fn in callbacks:
            try:
                ret = await fn(ctx)
            except Exception as exc:  # noqa: BLE001 — hook isolation
                logger.exception("hook %s raised", name)
                raise HookError(name=name, cause=exc) from exc
            if ret is HOOK_SKIP:
                if name not in _SKIP_ALLOWED:
                    raise RuntimeError(
                        f"hook {name!r} returned HOOK_SKIP but skip is not "
                        f"permitted at this site (skip-allowed hooks: "
                        f"{sorted(_SKIP_ALLOWED)})"
                    )
                return HOOK_SKIP
            if ret is not None:
                last = ret
        return last


# ---------------------------------------------------------------------------
# ChatLoop
# ---------------------------------------------------------------------------


class ChatLoop:
    """The framework's canonical Think-Act-Observe loop.

    See ``spec/runtime/chat-loop.md`` for the design contract. Construction is
    cheap; ``run()`` does all the work. A ``ChatLoop`` instance is single-use
    (calling ``run()`` twice on the same instance is undefined behaviour).
    """

    def __init__(self, config: LoopConfig) -> None:
        self._config = config
        self._hooks = _HookRegistry()
        self._loop_id = uuid.uuid4().hex
        self._session_id = (
            config.session_id
            if config.session_id is not None
            else f"sess_{uuid.uuid4().hex[:12]}"
        )
        self._trace_id = f"tr_{uuid.uuid4().hex[:16]}"
        # Mutable per-run state, copied from the immutable config seed.
        self._state: dict[str, Any] = dict(config.initial_state)

    # ------------------------------------------------------------------
    # Public properties (read-only views for callers / tests)
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def trace_id(self) -> str:
        return self._trace_id

    @property
    def loop_id(self) -> str:
        return self._loop_id

    # ------------------------------------------------------------------
    # Hook registration
    # ------------------------------------------------------------------

    def on(self, name: HookName, callback: HookCallback) -> None:
        """Register a hook callback.

        Multiple callbacks per hook are allowed; they run in registration
        order. Raises ``ValueError`` if ``name`` is not one of the 11 known
        hook names.
        """
        self._hooks.register(name, callback)

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    async def run(self) -> AsyncIterator[SSEEvent]:
        """Execute the loop and yield SSE events.

        **A1.5a additions.** Two loop-owned safety nets:

        * ``LoopConfig.max_tool_result_bytes`` — every tool message body
          handed to the LLM is funneled through ``_truncate_oversized``; a
          payload over the cap is replaced with
          ``{"truncated": True, "original_bytes": N, "preview": "<prefix>"}``.
          The original ``ToolResult`` (and the ``after_tool_result`` ctx)
          are untouched.
        * ``LoopConfig.max_elapsed_seconds`` — round-entry wall-clock guard
          using ``time.monotonic()``. Tripping fires ``budget_exhausted``
          with ``limit_kind="time"``; the loop never aborts an in-flight
          LLM stream or tool dispatch mid-call.

        **A1.4 scope.** Activates the remaining 3 hooks and the
        cancellation path:

        * **emit** — every ``SSEEvent`` is funneled through ``_emit()`` which
          fires the hook with an ``EmitCtx``; callbacks may rewrite the event
          (return ``SSEEvent``), mutate it in place, or return ``HOOK_SKIP``
          to suppress emission entirely.
        * **error** — fires only on *framework infrastructure* failures:
          (a) LLM-stream raises (fatal — loop exits with
          ``final_status="failed"``) or (b) ``ToolRouter.dispatch`` itself
          raises (recoverable — loop synthesises a fail ``ToolResult`` and
          continues, decide_completion makes the call). A *business* tool
          raising is caught and wrapped by ``ToolRouter.dispatch`` into
          ``ToolResult(success=False, error=...)``; that path does NOT fire
          ``error`` because ``after_tool_result`` already exposes the failure
          via ``result.error``. The ``error`` hook is reserved for
          "framework infrastructure broke", not "the user's tool returned a
          structured failure".
        * **budget_exhausted** — fires on each of the four exhaustion paths
          (round-entry step debit, decide_completion's tokens/steps/tool_calls
          verdict, and the ``max_rounds`` for-else clause). A matching
          ``agent.event=budget_exhausted`` SSE is emitted alongside.
        * **CancelledError** — caller cancels the task running ``run()`` →
          the loop catches ``asyncio.CancelledError`` once, fires ``loop_end``
          with ``final_status="cancelled"``, emits a final ``session.end``,
          then re-raises so the cancellation contract is preserved.

        **A1.3 inheritance.** Harness integration on top of the A1.2 round body:

        * ``consume_budget`` is called at three points per round —
          (i) on round entry (``step=True``) to pre-debit one step,
          (ii) after the LLM stream with the round's total tokens, and
          (iii) once per dispatched tool call. The returned ``BudgetState``
          is threaded into ``decide_completion``.
        * ``decide_completion`` runs at the end of every round and replaces
          A1.2's "no tool_calls → stop" branch. Its dict form populates
          ``RoundEndCtx.decision``; the loop breaks when
          ``decision.status != "executing"``.
        * Hitting ``max_rounds`` without a non-executing decision is
          reported as ``budget_exhausted`` with ``limit_kind="rounds"`` —
          the only ``limit_kind`` ChatLoop itself owns (the harness budget
          owns tokens / steps / tool_calls).

        Still **not** in scope (later slices):

        * Round-level SSE events beyond the session envelope, ``error``,
          ``budget_exhausted`` (no ``round.start`` / ``tool.result`` etc.) —
          downstream callers emit those via the ``emit`` hook or by yielding
          synthetic events from ``after_assistant_message`` /
          ``after_tool_result``.
        * Retry wrapping around LLM + tool dispatch (``RetryPolicy`` is
          accepted on ``LoopConfig`` but currently unused) — A1.5.
        * ``HarnessTrace`` persistence, provider shape translation,
          ``max_tool_result_bytes`` truncation — A1.5.
        """
        # session_start (passes through ``emit`` hook → may be rewritten or
        # suppressed by callbacks; ``None`` means "suppress this emission").
        sse = await self._emit(
            SSEEvent(
                type="agent",
                event="session.start",
                payload={
                    "sessionId": self._session_id,
                    "traceId": self._trace_id,
                },
            )
        )
        if sse is not None:
            yield sse

        # Trace recorder — A1.5c. Lifecycle is tied to a single ``run()`` call,
        # never reused across runs. ``storage=None`` makes every method a
        # no-op so the rest of ``run()`` doesn't need null checks. Errors
        # while writing to storage are logged and disable the recorder for
        # the remainder of this run; they never propagate.
        recorder = _TraceRecorder(
            self._config.storage,
            trace_id=self._trace_id,
            session_id=self._session_id,
            provider_model=getattr(self._config.provider, "model", None),
        )
        await recorder.start_loop()

        # loop_start hook
        await self._hooks.fire(
            "loop_start",
            LoopStartCtx(
                **self._make_ctx_base(),
                initial_messages=list(self._config.initial_messages),
                initial_tools=self._resolve_tools(),
            ),
        )

        # Working buffers for the round body. ``tools`` lives here (not just
        # in send_ctx) so ``before_round`` hooks can grow/shrink the tool set
        # for the lifetime of the loop, not just for one round's API call.
        messages: list[LLMMessage] = list(self._config.initial_messages)
        tools: list[dict[str, Any]] = self._resolve_tools()
        aggregated_usage = LLMUsage()
        budget_state = BudgetState()
        rounds_completed = 0
        final_status: CompletionStatus = "completed"
        final_decision_payload: dict[str, Any] | None = None
        decision: CompletionDecision | None = None

        # ``failed_llm_exc`` holds a fatal LLM-stream exception captured for
        # logging in ``loop_end``. The exception itself is NOT re-raised — the
        # loop owns the contract that a failed run still emits a clean SSE
        # envelope (error event, then done, then session.end).
        failed_llm_exc: BaseException | None = None
        budget_exhaust_handled = False  # set when round-entry step debit trips

        # Wall-clock start anchor for ``max_elapsed_seconds``. We use
        # ``time.monotonic()`` so clock jumps (NTP, suspended laptops) don't
        # spuriously trigger the cap. Checked at round entry — the loop never
        # interrupts an in-flight LLM stream or tool dispatch mid-call.
        wall_start = time.monotonic()

        try:
            for round_idx in range(self._config.max_rounds):
                # 0. Wall-clock guard: ``max_elapsed_seconds`` is the only
                # loop-owned dimension that is time-based (the harness budget
                # is purely usage-based). Checked first so a runaway tool
                # chain that already exceeded the wall clock cannot squeeze
                # in another LLM call by happening to pre-debit cleanly.
                if (
                    self._config.max_elapsed_seconds is not None
                    and self._config.max_elapsed_seconds > 0
                ):
                    elapsed = time.monotonic() - wall_start
                    if elapsed > self._config.max_elapsed_seconds:
                        decision = CompletionDecision(
                            status="budget_exhausted",
                            reason=(
                                f"elapsed={elapsed:.2f}s "
                                f"> max_elapsed_seconds="
                                f"{self._config.max_elapsed_seconds}"
                            ),
                            limit_kind="time",
                        )
                        final_status = "budget_exhausted"
                        final_decision_payload = decision.to_dict()
                        async for sse in self._fire_budget_exhausted(
                            limit_kind="time",
                            budget_state=budget_state,
                            recorder=recorder,
                        ):
                            yield sse
                        budget_exhaust_handled = True
                        break

                # 1. Pre-debit one step against the harness budget. If this
                # trips the limit we bail out before spending an LLM call.
                if self._config.budget is not None:
                    budget_state, would_exhaust = consume_budget(
                        budget_state, self._config.budget, step=True
                    )
                    if would_exhaust:
                        decision = CompletionDecision(
                            status="budget_exhausted",
                            reason=(
                                f"steps_used={budget_state.steps_used} "
                                f"> max_steps={self._config.budget.max_steps}"
                            ),
                            limit_kind="steps",
                        )
                        final_status = "budget_exhausted"
                        final_decision_payload = decision.to_dict()
                        async for sse in self._fire_budget_exhausted(
                            limit_kind="steps",
                            budget_state=budget_state,
                            recorder=recorder,
                        ):
                            yield sse
                        budget_exhaust_handled = True
                        break

                # Open the round trace span. ``begin_round`` records start_ms
                # so children (llm_stream / tool spans) get the correct
                # parent. No-op when storage is None.
                recorder.begin_round(round_idx)

                # 2. before_round — hooks may grow ``messages`` (e.g. inject a
                # tool reality check) or ``tools`` (e.g. expose new MCP tools)
                # for this and subsequent rounds.
                await self._hooks.fire(
                    "before_round",
                    RoundStartCtx(
                        **self._make_ctx_base(),
                        round_index=round_idx,
                        messages=messages,
                        tools=tools,
                    ),
                )

                # 3. before_send_messages gets a *copy* of messages/tools so
                # callbacks can tweak the payload for this single API call
                # (system prompt, context window trimming, temperature override)
                # without affecting the loop-wide buffers.
                send_ctx = SendMessagesCtx(
                    **self._make_ctx_base(),
                    messages=list(messages),
                    tools=list(tools),
                    model=self._config.provider.model,
                    provider_kind=self._config.provider_kind,
                    temperature=self._config.temperature,
                    max_tokens=self._config.max_tokens,
                )
                await self._hooks.fire("before_send_messages", send_ctx)

                # 4. LLM stream — accumulate content / reasoning / tool_calls /
                # usage / finish_reason. Exceptions here are fatal: we fire the
                # ``error`` hook, emit an ``agent.event=error`` SSE, mark the
                # loop ``failed``, and break out of the round body.
                # ``CancelledError`` is intentionally NOT caught here; it
                # propagates up to the outer ``try`` so cancellation is
                # handled exactly once.
                content_parts: list[str] = []
                reasoning_parts: list[str] = []
                tool_calls_acc: dict[str, ToolCall] = {}
                round_usage: LLMUsage | None = None
                finish_reason: str | None = None

                llm_start_ms = _now_ms()
                try:
                    async for chunk in self._stream_with_retry(
                        messages=send_ctx.messages,
                        tools=send_ctx.tools,
                        temperature=send_ctx.temperature,
                        max_tokens=send_ctx.max_tokens,
                        round_idx=round_idx,
                    ):
                        if chunk.content_delta:
                            content_parts.append(chunk.content_delta)
                        if chunk.reasoning_delta:
                            reasoning_parts.append(chunk.reasoning_delta)
                        if chunk.tool_call_delta is not None:
                            _accumulate_tool_call(tool_calls_acc, chunk.tool_call_delta)
                        if chunk.usage is not None:
                            round_usage = chunk.usage
                        if chunk.finish_reason:
                            finish_reason = chunk.finish_reason
                except asyncio.CancelledError:
                    raise
                except HookError:
                    # Hook errors raised mid-stream bubble through; the outer
                    # cancellation/finalisation handler does not catch them,
                    # so the caller still sees ``HookError`` — that's the
                    # contract: hook bugs are programming errors.
                    raise
                except Exception as exc:  # noqa: BLE001 — fatal-error path is broad on purpose
                    logger.exception(
                        "LLM stream failed: round=%d provider=%s",
                        round_idx,
                        self._config.provider.name,
                    )
                    recorder.record_llm_stream(
                        round_idx,
                        start_ms=llm_start_ms,
                        end_ms=_now_ms(),
                        usage=round_usage,
                        status="error",
                        finish_reason=finish_reason,
                    )
                    async for sse in self._fire_error(
                        exception=exc,
                        phase="llm_stream",
                        round_index=round_idx,
                        recorder=recorder,
                    ):
                        yield sse
                    failed_llm_exc = exc
                    decision = CompletionDecision(
                        status="failed",
                        reason=f"llm_stream_exception: {type(exc).__name__}",
                    )
                    final_status = "failed"
                    final_decision_payload = decision.to_dict()
                    recorder.end_round(
                        round_idx,
                        status="error",
                        decision=final_decision_payload,
                    )
                    await recorder.flush()
                    break

                recorder.record_llm_stream(
                    round_idx,
                    start_ms=llm_start_ms,
                    end_ms=_now_ms(),
                    usage=round_usage,
                    status="ok",
                    finish_reason=finish_reason,
                )

                assembled_calls: list[ToolCall] = list(tool_calls_acc.values())
                assistant_msg = LLMMessage(
                    role="assistant",
                    content="".join(content_parts),
                    tool_calls=assembled_calls or None,
                )
                messages.append(assistant_msg)
                reasoning_text = "".join(reasoning_parts) or None

                # 5. Aggregate usage across rounds + debit tokens against budget.
                if round_usage is not None:
                    aggregated_usage = LLMUsage(
                        prompt_tokens=aggregated_usage.prompt_tokens + round_usage.prompt_tokens,
                        completion_tokens=(
                            aggregated_usage.completion_tokens + round_usage.completion_tokens
                        ),
                        total_tokens=aggregated_usage.total_tokens + round_usage.total_tokens,
                    )
                    if self._config.budget is not None and round_usage.total_tokens > 0:
                        # ``consume_budget`` reports exhaustion but we don't
                        # act on it until ``decide_completion`` runs at the
                        # end of the round — that way ``after_assistant_message``
                        # + tool dispatch still see the truthful state and the
                        # decision is reported once, not twice.
                        budget_state, _ = consume_budget(
                            budget_state,
                            self._config.budget,
                            tokens=round_usage.total_tokens,
                        )

                # 6. after_assistant_message
                await self._hooks.fire(
                    "after_assistant_message",
                    AssistantMessageCtx(
                        **self._make_ctx_base(),
                        message=assistant_msg,
                        reasoning=reasoning_text,
                        usage=_usage_to_dict(round_usage) if round_usage else None,
                    ),
                )

                rounds_completed = round_idx + 1

                # 7. Tool dispatch — one debit per dispatched call (HOOK_SKIPped
                # calls still count, since the model proposed them and they
                # consume orchestration budget).
                tool_results: list[ToolResult] = []
                for call in assembled_calls:
                    tc_ctx = ToolCallCtx(
                        **self._make_ctx_base(),
                        tool_call=call,
                        round_index=round_idx,
                    )
                    skip_ret = await self._hooks.fire("before_tool_call", tc_ctx)

                    tool_start_ms = _now_ms()
                    if skip_ret is HOOK_SKIP:
                        # Synthesise a stub result so the model still sees one
                        # tool message per outstanding tool_call in round n+1.
                        # ``needsFollowup=True`` keeps ``decide_completion``
                        # from interpreting the stub as a terminal failure —
                        # skipping is "handle this elsewhere, then carry on",
                        # not "this tool gave up".
                        result = ToolResult(
                            success=False,
                            message="Tool dispatch skipped by hook",
                            terminal=False,
                            needsFollowup=True,
                        )
                        effective_call = call
                    else:
                        effective_call = tc_ctx.tool_call  # type: ignore[assignment]
                        try:
                            result = await self._config.tool_router.dispatch(effective_call)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001 — recoverable error path
                            logger.exception(
                                "tool dispatch failed: tool=%s round=%d",
                                effective_call.name,
                                round_idx,
                            )
                            # Fire ``error`` hook + emit SSE. ``tool_dispatch``
                            # is a *recoverable* phase: the loop synthesises a
                            # fail ``ToolResult`` (needsFollowup=True) and
                            # lets ``decide_completion`` decide.
                            async for sse in self._fire_error(
                                exception=exc,
                                phase="tool_dispatch",
                                round_index=round_idx,
                                recorder=recorder,
                            ):
                                yield sse
                            result = ToolResult(
                                success=False,
                                error=str(exc),
                                terminal=False,
                                needsFollowup=True,
                            )

                    recorder.record_tool_call(
                        round_idx,
                        call=effective_call,
                        result=result,
                        start_ms=tool_start_ms,
                        end_ms=_now_ms(),
                    )
                    tool_results.append(result)

                    if self._config.budget is not None:
                        budget_state, _ = consume_budget(
                            budget_state, self._config.budget, tool_call=True
                        )

                    await self._hooks.fire(
                        "after_tool_result",
                        ToolResultCtx(
                            **self._make_ctx_base(),
                            tool_call=call,
                            tool_result=result,
                            round_index=round_idx,
                        ),
                    )

                    # Truncate oversized tool content *for the LLM only*; the
                    # ``result`` object itself (and the ``after_tool_result``
                    # hook ctx) remain unchanged so downstream observers see
                    # the full payload.
                    serialised = _serialise_tool_result(result)
                    serialised = _truncate_oversized(
                        serialised, self._config.max_tool_result_bytes
                    )
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=serialised,
                            tool_call_id=call.id,
                            name=call.name,
                        )
                    )

                # 8. End-of-round decision. ``decide_completion`` is the single
                # gating predicate; the natural-stop branch from A1.2 is folded
                # into ``status="completed"`` via ``reason="no_tool_calls"``.
                decision = decide_completion(
                    tool_calls=[call.model_dump() for call in assembled_calls],
                    tool_results=[r.model_dump() for r in tool_results],
                    budget_state=budget_state,
                    budget_limits=self._config.budget,
                    finish_reason=finish_reason,
                )

                await self._hooks.fire(
                    "after_round",
                    RoundEndCtx(
                        **self._make_ctx_base(),
                        round_index=round_idx,
                        assistant_message=assistant_msg,
                        tool_calls=assembled_calls,
                        tool_results=tool_results,
                        decision=decision.to_dict(),
                        finish_reason=finish_reason,
                    ),
                )

                # Close the round trace span with the decision status mapped
                # into the span vocabulary. We do this *before* the
                # ``budget_exhausted`` SSE so the span ordering matches the
                # event ordering downstream observers see.
                round_span_status = (
                    "ok" if decision.status in ("executing", "completed") else "error"
                )
                recorder.end_round(
                    round_idx,
                    status=round_span_status,
                    decision=decision.to_dict(),
                )
                await recorder.flush()

                if decision.status != "executing":
                    final_status = decision.status
                    final_decision_payload = decision.to_dict()
                    if decision.status == "budget_exhausted":
                        async for sse in self._fire_budget_exhausted(
                            limit_kind=decision.limit_kind or "unknown",
                            budget_state=budget_state,
                            recorder=recorder,
                        ):
                            yield sse
                        budget_exhaust_handled = True
                        await recorder.flush()
                    break
            else:
                # for-else: ran the full ``max_rounds`` range without ``break``.
                # The model is still asking for tool calls but ChatLoop's own
                # round cap won't allow another iteration. ``limit_kind="rounds"``
                # is reported here (not by the harness, which doesn't know about
                # ``max_rounds`` — that's a framework-loop concept).
                decision = CompletionDecision(
                    status="budget_exhausted",
                    reason=f"max_rounds={self._config.max_rounds} reached",
                    limit_kind="rounds",
                )
                final_status = "budget_exhausted"
                final_decision_payload = decision.to_dict()
                async for sse in self._fire_budget_exhausted(
                    limit_kind="rounds",
                    budget_state=budget_state,
                    recorder=recorder,
                ):
                    yield sse
                budget_exhaust_handled = True
        except asyncio.CancelledError:
            # Caller cancelled the task running ``run()``. The contract is:
            # fire ``loop_end`` with ``final_status="cancelled"``, emit a final
            # ``session.end``, then re-raise so the cancellation propagates
            # naturally. Re-raising preserves ``asyncio.shield`` semantics
            # and keeps the loop honest with structured-concurrency callers.
            final_status = "cancelled"
            final_decision_payload = {
                "status": "cancelled",
                "reason": "cancelled_by_caller",
                "limit_kind": None,
                "terminal_index": None,
            }
            # Best-effort: fire loop_end, emit session.end, finalise trace.
            # If a hook (or storage) raises during cancellation we swallow
            # and continue to re-raise the CancelledError — losing user
            # code's bug here is acceptable to preserve cancellation
            # semantics.
            try:
                recorder.record_cancelled()
                await self._hooks.fire(
                    "loop_end",
                    LoopEndCtx(
                        **self._make_ctx_base(),
                        final_status=final_status,
                        final_decision=final_decision_payload,
                        rounds_completed=rounds_completed,
                        total_usage=(
                            _usage_to_dict(aggregated_usage)
                            if aggregated_usage.total_tokens > 0
                            else None
                        ),
                    ),
                )
                sse = await self._emit(
                    SSEEvent(
                        type="agent",
                        event="session.end",
                        payload={"finalStatus": final_status},
                    )
                )
                if sse is not None:
                    yield sse
                await recorder.end_loop(final_status=final_status)
            except Exception:  # noqa: BLE001 — finalisation is best-effort under cancel
                logger.exception("loop_end / session.end during cancellation failed")
            raise

        # ``budget_exhaust_handled`` keeps us honest: the hook + SSE fire
        # exactly once per run, even though there are four code paths that
        # can produce a ``budget_exhausted`` final status.
        del budget_exhaust_handled

        # done
        sse = await self._emit(SSEEvent(type="done"))
        if sse is not None:
            yield sse

        # loop_end hook — ``final_decision`` carries the dict form of the
        # ``CompletionDecision`` that ended the loop (or the synthetic
        # ``limit_kind="rounds"`` decision on max_rounds exhaustion).
        await self._hooks.fire(
            "loop_end",
            LoopEndCtx(
                **self._make_ctx_base(),
                final_status=final_status,
                final_decision=final_decision_payload,
                rounds_completed=rounds_completed,
                total_usage=(
                    _usage_to_dict(aggregated_usage)
                    if aggregated_usage.total_tokens > 0
                    else None
                ),
            ),
        )

        # session_end
        sse = await self._emit(
            SSEEvent(
                type="agent",
                event="session.end",
                payload={"finalStatus": final_status},
            )
        )
        if sse is not None:
            yield sse

        # Finalise the HarnessTrace record. ``end_loop`` is internally
        # best-effort; storage failure here is logged and swallowed so it
        # never masks the loop's actual outcome.
        await recorder.end_loop(final_status=final_status)

        if failed_llm_exc is not None:
            # The LLM-stream failure path emitted ``agent.event=error``,
            # ``done``, and ``session.end`` for the SSE envelope; here we
            # surface a structured log entry so operators see the original
            # cause. The exception itself is intentionally NOT re-raised —
            # callers get the failure via ``LoopEndCtx.final_status``.
            logger.error(
                "ChatLoop ended in 'failed' state due to LLM-stream "
                "exception: %r",
                failed_llm_exc,
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _make_ctx_base(self) -> dict[str, Any]:
        """The kwargs every HookContext subclass takes."""
        return {
            "loop_id": self._loop_id,
            "session_id": self._session_id,
            "trace_id": self._trace_id,
            "config": self._config,
            "state": self._state,
            "storage": self._config.storage,
        }

    async def _fire_budget_exhausted(
        self,
        *,
        limit_kind: str,
        budget_state: BudgetState,
        recorder: "_TraceRecorder | None" = None,
    ) -> AsyncIterator[SSEEvent]:
        """Fire ``budget_exhausted`` hook and yield (via ``_emit``) the matching
        ``agent.event=budget_exhausted`` SSE.

        Centralised so all four exhaustion paths (round-entry step debit,
        decide_completion's tokens / steps / tool_calls verdict, and the
        ``max_rounds`` for-else) emit identically. When a ``recorder`` is
        supplied (A1.5c), the matching ``budget.exhausted`` trace event is
        also captured.
        """
        state_dict = _budget_state_to_dict(budget_state)
        await self._hooks.fire(
            "budget_exhausted",
            BudgetExhaustedCtx(
                **self._make_ctx_base(),
                limit_kind=limit_kind,
                budget_state=state_dict,
            ),
        )
        if recorder is not None:
            recorder.record_budget_exhausted(
                limit_kind=limit_kind, budget_state=state_dict
            )
        sse = await self._emit(
            SSEEvent(
                type="agent",
                event="budget_exhausted",
                payload={"limitKind": limit_kind, "budgetState": state_dict},
            )
        )
        if sse is not None:
            yield sse

    async def _fire_error(
        self,
        *,
        exception: BaseException,
        phase: Literal["llm_stream", "tool_dispatch", "hook"],
        round_index: int,
        recorder: "_TraceRecorder | None" = None,
    ) -> AsyncIterator[SSEEvent]:
        """Fire ``error`` hook and yield (via ``_emit``) an
        ``agent.event=error`` SSE.

        Used for both fatal LLM-stream errors (caller breaks the loop after)
        and recoverable tool-dispatch errors (caller continues with a
        synthesised fail ``ToolResult``). When a ``recorder`` is supplied
        (A1.5c), the trace event is captured alongside the SSE.
        """
        await self._hooks.fire(
            "error",
            ErrorCtx(
                **self._make_ctx_base(),
                exception=exception,
                phase=phase,
                round_index=round_index,
            ),
        )
        if recorder is not None:
            recorder.record_error(
                exception=exception, phase=phase, round_idx=round_index
            )
        sse = await self._emit(
            SSEEvent(
                type="agent",
                event="error",
                payload={
                    "phase": phase,
                    "roundIndex": round_index,
                    "errorType": type(exception).__name__,
                    "message": str(exception),
                },
            )
        )
        if sse is not None:
            yield sse

    async def _stream_with_retry(
        self,
        *,
        messages: Sequence[LLMMessage],
        tools: Sequence[dict[str, Any]] | None,
        temperature: float | None,
        max_tokens: int | None,
        round_idx: int,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Wrap ``provider.stream(...)`` with retry-on-startup.

        Retry coverage is intentionally narrow: it spans **stream creation**
        plus **awaiting the first chunk**. Once any chunk has been yielded
        downstream the loop has already started mutating accumulators
        (content / reasoning / tool_calls / usage), so silently retrying
        would either duplicate or lose state. Therefore mid-stream
        exceptions bubble up unchanged.

        Behaviour without ``retry_policy`` (or ``max_attempts <= 1``) is
        equivalent to calling ``provider.stream(...)`` directly — there is
        no observable difference, no extra ``await``, no extra log.

        Retry decisions use ``steerable_agent_harness.is_retryable_error``;
        anything not classified as retryable, plus ``CancelledError`` and
        ``HookError``, propagates immediately. ``HookError`` is not retried
        because it indicates a bug in user callbacks, not a transient
        failure.

        Retries are silent w.r.t. the ``error`` hook: only the *final*
        failure (retries exhausted, or non-retryable exception) reaches the
        hook via the regular fatal path in ``run()``. Individual attempts
        are logged at ``WARNING``.
        """
        policy = self._config.retry_policy
        max_attempts = max(1, policy.max_attempts) if policy is not None else 1

        tool_list = list(tools) if tools is not None else None
        for attempt in range(1, max_attempts + 1):
            try:
                stream = self._config.provider.stream(
                    messages,
                    tools=tool_list,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                stream_iter = stream.__aiter__()
                try:
                    first_chunk = await stream_iter.__anext__()
                except StopAsyncIteration:
                    # Empty stream — natural finish, nothing to retry.
                    return
            except asyncio.CancelledError:
                raise
            except HookError:
                raise
            except Exception as exc:  # noqa: BLE001 — classified below
                if (
                    policy is None
                    or attempt >= max_attempts
                    or not is_retryable_error(exc)
                ):
                    raise
                delay_ms = next_retry_delay_ms(policy, attempt)
                logger.warning(
                    "LLM stream attempt %d/%d failed for round=%d "
                    "provider=%s: %s (%s); retrying in %dms",
                    attempt,
                    max_attempts,
                    round_idx,
                    self._config.provider.name,
                    type(exc).__name__,
                    exc,
                    delay_ms,
                )
                if delay_ms > 0:
                    await asyncio.sleep(delay_ms / 1000)
                continue

            # First chunk in hand — yield it, then drain the rest verbatim.
            yield first_chunk
            async for chunk in stream_iter:
                yield chunk
            return

    async def _emit(self, event: SSEEvent) -> SSEEvent | None:
        """Funnel an outgoing ``SSEEvent`` through the ``emit`` hook.

        Returns the (possibly rewritten) event to yield, or ``None`` if a
        callback returned ``HOOK_SKIP`` to suppress the event entirely.

        The hook may:

        * mutate ``ctx.event`` in place — we read it back after firing
        * return a new ``SSEEvent`` to replace it (``ctx.event`` is ignored
          if a non-None ``SSEEvent`` was returned)
        * return ``HOOK_SKIP`` — the event is dropped, downstream sees
          nothing for this emission

        ``HookError`` from a faulty callback propagates: emit handlers are
        user code and bugs there should surface, not be silently swallowed.
        """
        ctx = EmitCtx(**self._make_ctx_base(), event=event)
        ret = await self._hooks.fire("emit", ctx)
        if ret is HOOK_SKIP:
            return None
        if isinstance(ret, SSEEvent):
            return ret
        # Default path: either no hook ran, callbacks returned None, or they
        # returned something non-SSEEvent (which we treat as "no replacement").
        # In all cases the canonical event is whatever ctx.event currently
        # holds — in-place mutation is supported.
        return ctx.event

    def _resolve_tools(self) -> list[dict[str, Any]]:
        if self._config.tool_descriptors is not None:
            return list(self._config.tool_descriptors)
        return self._config.tool_router.describe()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _accumulate_tool_call(acc: dict[str, ToolCall], delta: ToolCall) -> None:
    """Merge a streamed ``tool_call_delta`` into the per-id accumulator.

    Provider layers stream tool calls in multiple chunks. The id is the merge
    key:

    * If ``delta.id`` is empty, the chunk is treated as a continuation of the
      most recently opened call (OpenAI convention: id appears only in the
      first chunk).
    * Otherwise, an existing entry with the same id is merged in place:
      ``name`` uses last-non-empty; ``arguments`` uses last-wins because the
      provider layer ``json.loads`` the full args string per chunk and falls
      back to ``{}`` on partial JSON, so the last successful parse is
      authoritative. The partial-args streaming fix is A1.5.

    The order of entries in ``acc`` mirrors the order calls were first opened,
    which is what every provider expects when re-sending the assistant message.
    """
    if not delta.id:
        if not acc:
            # No open call to attach to. This is a provider bug (a tool_call
            # delta without an id and before any call has been opened) — log
            # it and drop the delta so the loop doesn't crash mid-stream.
            logger.warning(
                "tool_call_delta with empty id dropped (no open call to "
                "continue): name=%r arguments=%r",
                delta.name,
                delta.arguments,
            )
            return
        last_key = next(reversed(acc))
        existing = acc[last_key]
        if delta.name:
            existing.name = delta.name
        if delta.arguments:
            existing.arguments = {**existing.arguments, **delta.arguments}
        return

    existing = acc.get(delta.id)
    if existing is None:
        acc[delta.id] = ToolCall(
            id=delta.id,
            name=delta.name or "",
            arguments=dict(delta.arguments) if delta.arguments else {},
        )
        return
    if delta.name:
        existing.name = delta.name
    if delta.arguments:
        existing.arguments = dict(delta.arguments)


def _serialise_tool_result(result: ToolResult) -> str:
    """Render a ``ToolResult`` into the ``content`` of a tool message.

    Convention:

    * If ``result.message`` is set, use it verbatim (covers human-readable
      summaries from tool handlers).
    * Otherwise dump a compact JSON object with ``success`` plus whichever of
      ``error`` / ``data`` are populated.

    Truncation against ``LoopConfig.max_tool_result_bytes`` is applied
    separately by ``_truncate_oversized`` after serialisation; this lets
    ``after_tool_result`` hooks observe the original full ``ToolResult``
    while only the LLM-visible content is shrunk.
    """
    if result.message:
        return result.message
    payload: dict[str, Any] = {"success": result.success}
    if result.error:
        payload["error"] = result.error
    if result.data:
        payload["data"] = result.data
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _truncate_oversized(serialised: str, max_bytes: int) -> str:
    """Wrap an oversized tool-result string in a ``truncated`` envelope.

    Returns ``serialised`` unchanged if its UTF-8 length is within
    ``max_bytes``. Otherwise emits a JSON object of the form
    ``{"truncated": True, "original_bytes": N, "preview": "<prefix>"}``
    where ``preview`` keeps roughly the first 70% of ``max_bytes`` as a
    cleanly-UTF-8-decoded prefix.

    Design intent: protect the prompt budget from a single rogue tool-call
    blowing past the context window, *while still showing the model enough
    of the payload to recognise what was truncated*. Stronger compression
    (RAG summarisation, structured field selection) belongs in
    ``after_tool_result`` hooks — this is the last-resort safety net.

    ``max_bytes <= 0`` disables truncation entirely (the caller's "no cap"
    convention).
    """
    if max_bytes <= 0:
        return serialised
    raw = serialised.encode("utf-8")
    if len(raw) <= max_bytes:
        return serialised
    preview_bytes = max(1, int(max_bytes * 0.7))
    # ``errors="ignore"`` drops a half-multibyte codepoint at the cut, so the
    # resulting string is always valid UTF-8.
    preview = raw[:preview_bytes].decode("utf-8", errors="ignore")
    return json.dumps(
        {
            "truncated": True,
            "original_bytes": len(raw),
            "preview": preview,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _usage_to_dict(usage: LLMUsage) -> dict[str, int]:
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }


def _budget_state_to_dict(state: BudgetState) -> dict[str, int]:
    """Serialise a ``BudgetState`` for hook contexts and SSE payloads."""
    return {
        "tokens_used": state.tokens_used,
        "steps_used": state.steps_used,
        "tool_calls_used": state.tool_calls_used,
    }


def _now_ms() -> int:
    """Wall-clock millis. Used for span/event timestamps where users need
    real time, not the monotonic clock that gates ``max_elapsed_seconds``."""
    return int(time.time() * 1000)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# _TraceRecorder (A1.5c)
# ---------------------------------------------------------------------------


class _TraceRecorder:
    """Per-run trace recorder. Buffers spans / events and flushes per round.

    Design contract:

    * **Optional** — when ``storage is None``, every method is a no-op and
      no extra ``await`` is paid. The loop runs identically to A1.4 / A1.5a.
    * **Best-effort** — storage failures are logged at ``WARNING`` and
      disable further writes for the remainder of the run; they never
      propagate up to the caller. Tracing must not break the loop.
    * **Per-round flush** — spans + events captured during a round are
      flushed at ``end_round``. Lifecycle events (``loop.start``,
      ``loop.end``, ``error``, ``budget.exhausted``, ``cancelled``) flush
      at the next opportunity.
    * **Span hierarchy** — outer ``loop`` span; per-round ``round`` span
      (parent=loop); ``llm_stream`` and ``tool`` spans (parent=round).

    Vocabulary (subject to evolution; documented in chat-loop.md §9.7):

    Span ``kind``: ``"loop"`` | ``"round"`` | ``"llm"`` | ``"tool"``.
    Event ``kind``: ``"lifecycle"`` | ``"round"`` | ``"error"`` |
    ``"budget_exhausted"`` | ``"cancellation"``.
    """

    def __init__(
        self,
        storage: StorageAdapter | None,
        *,
        trace_id: str,
        session_id: str,
        provider_model: str | None,
    ) -> None:
        self._storage = storage
        self._enabled = storage is not None
        self._trace_id = trace_id
        self._session_id = session_id
        self._provider_model = provider_model
        self._sequence = 0
        self._loop_span_id = uuid.uuid4().hex
        self._loop_start_ms = 0
        self._created_at_iso = _now_iso()
        # round_idx -> (span_id, start_ms)
        self._round_meta: dict[int, tuple[str, int]] = {}
        self._pending_spans: list[TraceSpan] = []
        self._pending_events: list[TraceEvent] = []
        self._total_tokens = 0
        self._had_error = False
        self._error_message: str | None = None
        self._span_count = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _next_seq(self) -> int:
        seq = self._sequence
        self._sequence += 1
        return seq

    def _emit_event(
        self,
        *,
        kind: str,
        name: str,
        payload: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> None:
        if not self._enabled:
            return
        self._pending_events.append(
            TraceEvent(
                traceId=self._trace_id,
                kind=kind,
                name=name,
                sequence=self._next_seq(),
                timestampMs=_now_ms(),
                status=status,
                payload=payload,
            )
        )

    async def start_loop(self) -> None:
        if not self._enabled:
            return
        self._loop_start_ms = _now_ms()
        skeleton = HarnessTrace(
            traceId=self._trace_id,
            sessionId=self._session_id,
            status="running",
            hadError=False,
            eventCount=0,
            spanCount=0,
            modelId=self._provider_model,
            startedAtMs=self._loop_start_ms,
            createdAt=self._created_at_iso,
            updatedAt=self._created_at_iso,
        )
        try:
            assert self._storage is not None
            await self._storage.upsert_trace(skeleton)
        except Exception as exc:  # noqa: BLE001 — storage must not break the loop
            logger.warning(
                "_TraceRecorder.start_loop: storage upsert_trace failed "
                "(%s: %s); disabling further trace writes for this run",
                type(exc).__name__,
                exc,
            )
            self._enabled = False
            return
        self._emit_event(kind="lifecycle", name="loop.start", status="ok")

    def begin_round(self, round_idx: int) -> str:
        """Open a round span. Returns its ``spanId`` so children can set
        ``parentSpanId``. Returns the loop span id if disabled."""
        if not self._enabled:
            return self._loop_span_id
        span_id = uuid.uuid4().hex
        start_ms = _now_ms()
        self._round_meta[round_idx] = (span_id, start_ms)
        self._emit_event(
            kind="round",
            name="round.start",
            payload={"roundIndex": round_idx},
        )
        return span_id

    def end_round(
        self,
        round_idx: int,
        *,
        status: str,
        decision: dict[str, Any] | None = None,
    ) -> None:
        if not self._enabled:
            return
        meta = self._round_meta.get(round_idx)
        if meta is None:
            # ``begin_round`` wasn't called; nothing to close.
            return
        span_id, start_ms = meta
        end_ms = _now_ms()
        self._pending_spans.append(
            TraceSpan(
                spanId=span_id,
                traceId=self._trace_id,
                parentSpanId=self._loop_span_id,
                name=f"round.{round_idx}",
                kind="round",
                startMs=start_ms,
                endMs=end_ms,
                durationMs=end_ms - start_ms,
                status=status,
            )
        )
        self._span_count += 1
        self._emit_event(
            kind="round",
            name="round.end",
            payload={"roundIndex": round_idx, "decision": decision},
            status=status,
        )

    def record_llm_stream(
        self,
        round_idx: int,
        *,
        start_ms: int,
        end_ms: int,
        usage: LLMUsage | None,
        status: str,
        finish_reason: str | None = None,
    ) -> None:
        if not self._enabled:
            return
        parent_id = self._round_meta.get(round_idx, (self._loop_span_id, 0))[0]
        attrs: dict[str, Any] = {}
        if usage is not None:
            attrs.update(
                {
                    "promptTokens": usage.prompt_tokens,
                    "completionTokens": usage.completion_tokens,
                    "totalTokens": usage.total_tokens,
                }
            )
            self._total_tokens += usage.total_tokens
        if finish_reason:
            attrs["finishReason"] = finish_reason
        self._pending_spans.append(
            TraceSpan(
                spanId=uuid.uuid4().hex,
                traceId=self._trace_id,
                parentSpanId=parent_id,
                name="llm_stream",
                kind="llm",
                startMs=start_ms,
                endMs=end_ms,
                durationMs=end_ms - start_ms,
                status=status,
                attrs=attrs or None,
            )
        )
        self._span_count += 1

    def record_tool_call(
        self,
        round_idx: int,
        *,
        call: ToolCall,
        result: ToolResult | None,
        start_ms: int,
        end_ms: int,
    ) -> None:
        if not self._enabled:
            return
        parent_id = self._round_meta.get(round_idx, (self._loop_span_id, 0))[0]
        success = bool(result and result.success)
        attrs: dict[str, Any] = {
            "toolName": call.name,
            "toolCallId": call.id,
            "success": success,
        }
        if result is not None and not result.success and result.error:
            attrs["error"] = result.error
        self._pending_spans.append(
            TraceSpan(
                spanId=uuid.uuid4().hex,
                traceId=self._trace_id,
                parentSpanId=parent_id,
                name=f"tool:{call.name}" if call.name else "tool",
                kind="tool",
                startMs=start_ms,
                endMs=end_ms,
                durationMs=end_ms - start_ms,
                status="ok" if success else "error",
                attrs=attrs,
            )
        )
        self._span_count += 1

    def record_error(
        self,
        *,
        exception: BaseException,
        phase: str,
        round_idx: int,
    ) -> None:
        if not self._enabled:
            return
        self._had_error = True
        if self._error_message is None:
            self._error_message = f"{type(exception).__name__}: {exception}"
        self._emit_event(
            kind="error",
            name="error",
            status="error",
            payload={
                "phase": phase,
                "errorType": type(exception).__name__,
                "message": str(exception),
                "roundIndex": round_idx,
            },
        )

    def record_budget_exhausted(
        self,
        *,
        limit_kind: str,
        budget_state: dict[str, int],
    ) -> None:
        if not self._enabled:
            return
        self._emit_event(
            kind="budget_exhausted",
            name="budget.exhausted",
            payload={"limitKind": limit_kind, "budgetState": budget_state},
        )

    def record_cancelled(self) -> None:
        if not self._enabled:
            return
        self._emit_event(
            kind="cancellation",
            name="loop.cancelled",
            status="cancelled",
        )

    async def flush(self) -> None:
        """Push buffered spans + events to storage. Safe to call repeatedly.

        On storage failure this recorder disables further writes for the
        remainder of the run; the loop is not interrupted.
        """
        if not self._enabled:
            return
        if not self._pending_spans and not self._pending_events:
            return
        spans = self._pending_spans
        events = self._pending_events
        self._pending_spans = []
        self._pending_events = []
        try:
            assert self._storage is not None
            if spans:
                await self._storage.append_spans(self._trace_id, spans)
            if events:
                await self._storage.append_events(self._trace_id, events)
        except Exception as exc:  # noqa: BLE001 — storage must not break the loop
            logger.warning(
                "_TraceRecorder.flush: storage append failed "
                "(%s: %s); disabling further trace writes for this run",
                type(exc).__name__,
                exc,
            )
            self._enabled = False

    async def end_loop(
        self,
        *,
        final_status: str,
        error_message: str | None = None,
    ) -> None:
        if not self._enabled:
            return
        loop_end_ms = _now_ms()
        # Close the outer loop span. Map ChatLoop final_status onto a
        # span-status vocabulary that downstreams already understand.
        span_status = (
            "ok"
            if final_status == "completed"
            else "cancelled"
            if final_status == "cancelled"
            else "error"
        )
        self._pending_spans.append(
            TraceSpan(
                spanId=self._loop_span_id,
                traceId=self._trace_id,
                parentSpanId=None,
                name="ChatLoop.run",
                kind="loop",
                startMs=self._loop_start_ms,
                endMs=loop_end_ms,
                durationMs=loop_end_ms - self._loop_start_ms,
                status=span_status,
            )
        )
        self._span_count += 1
        self._emit_event(
            kind="lifecycle",
            name="loop.end",
            payload={"finalStatus": final_status},
            status=span_status,
        )
        await self.flush()
        # Finalise the HarnessTrace record. This is the last thing the
        # recorder does; whether it succeeds or not, the loop is already
        # finished (or cancelled) so failure is purely a tracing concern.
        try:
            assert self._storage is not None
            await self._storage.upsert_trace(
                HarnessTrace(
                    traceId=self._trace_id,
                    sessionId=self._session_id,
                    status=final_status,
                    durationMs=loop_end_ms - self._loop_start_ms,
                    hadError=self._had_error or final_status in ("failed", "cancelled"),
                    errorMessage=self._error_message or error_message,
                    eventCount=self._sequence,
                    spanCount=self._span_count,
                    totalTokens=self._total_tokens or None,
                    modelId=self._provider_model,
                    startedAtMs=self._loop_start_ms,
                    createdAt=self._created_at_iso,
                    updatedAt=_now_iso(),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "_TraceRecorder.end_loop: storage upsert_trace failed "
                "(%s: %s)",
                type(exc).__name__,
                exc,
            )


__all__ = [
    "ChatLoop",
    "LoopConfig",
    "ProviderKind",
    "CompletionStatus",
    "HookName",
    "HookCallback",
    "HOOK_SKIP",
    "HookError",
    "HookContext",
    "LoopStartCtx",
    "LoopEndCtx",
    "RoundStartCtx",
    "RoundEndCtx",
    "SendMessagesCtx",
    "AssistantMessageCtx",
    "ToolCallCtx",
    "ToolResultCtx",
    "EmitCtx",
    "BudgetExhaustedCtx",
    "ErrorCtx",
    # Re-exported from steerable_agent_harness for convenience.
    "BudgetLimit",
    "BudgetState",
    "CompletionDecision",
    "RetryPolicy",
    "consume_budget",
    "decide_completion",
]
