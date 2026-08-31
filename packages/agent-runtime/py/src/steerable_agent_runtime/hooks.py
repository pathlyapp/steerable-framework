"""Loop extension points — the seam that keeps CoreLoop small.

dsh hangs compaction / retry / approval off ``agent/pre-step`` and
``agent/request-error`` plugins so adding a capability never edits the loop.
CoreLoop takes the same shape with three hook points; each remaining A3 slice
(compaction, large-result spill, retry, …) lands as a hooks implementation
instead of more branches in ``loop.py``.

Hook points and their intended consumers:

- ``pre_step`` — before each LLM stream. Compaction declares its rewrite
  here; a hook may also append context (skill catalog) or reject the step
  (turn ends blocked).
- ``post_tool_result`` — after each tool execution, before the result enters
  the transcript. Large-result externalization rewrites oversized results to
  a preview + locator here.
- ``on_request_error`` — when the LLM stream raises. Retry policy decides
  retry-with-backoff vs fail here; the hook receives the current transcript
  so recovery strategies that rewrite it (context-overflow compaction) can
  declare the replacement alongside the retry decision.

Wave 1: hooks never mutate or replace the transcript directly. They return
*declarations* — ``TranscriptAppend`` (append-only growth) and
``RewriteRequest`` (the one declared rewrite) — and the loop applies them
through the ``ContextManager``, so the record stays append-only and every
rewrite lands as an auditable ``CompactionBoundary``.

All hooks default to no-op (``NoopHooks``), so wiring them in changes no
existing behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from steerable_agent_protocol.generated import ToolCall, ToolResult

if TYPE_CHECKING:
    from .history import ContextFragment
    from .llm import LLMMessage
    from .loop import LoopContext

# ---------------------------------------------------------------------------
# Action types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TranscriptAppend:
    """One append-only contribution from a hook.

    ``kind`` is the record's ``<feature>.<name>`` classification (defaults
    to the role-derived kind when None) so injected content is attributable
    in the record — e.g. the skill catalog lands as ``skills.catalog``.

    ``fragment`` carries the typed ``ContextFragment`` the message was
    rendered from; when present the loop appends via
    ``ContextManager.append_fragment`` so the fragment's token cap is
    enforced (P2.2). Raw messages without a fragment append unbounded —
    new injection surfaces should prefer carrying the fragment.
    """

    message: LLMMessage
    kind: str | None = None
    fragment: ContextFragment | None = None


@dataclass(frozen=True, slots=True)
class RewriteRequest:
    """The one declared rewrite: replace the visible projection wholesale.

    The loop applies it via ``ContextManager.replace_all``, which records a
    ``CompactionBoundary`` carrying ``reason`` / ``action`` — the record
    itself only grows. ``action`` is the ``hook_action`` label
    (``"compact"``, ``"overflow_recovery"``, …) so traces attribute the
    boundary without guessing.
    """

    messages: list[LLMMessage]
    reason: str
    action: str = "compact"


@dataclass(slots=True)
class PreStepAction:
    """Outcome of a ``pre_step`` hook.

    ``proceed`` (default) continues; the loop applies ``rewrite`` (declared
    wholesale replacement) then ``appends`` (append-only growth) to the
    record before the LLM call. ``reject`` ends the turn without calling
    the model.

    ``tool_choice`` (e.g. ``"required"``) is forwarded to the provider for
    this step's LLM call — the data-need router uses it to force a tool call
    on the first round of a data-seeking turn. Pass-through value, forwarded
    as a provider kwarg; providers that cannot honor it ignore it.
    """

    kind: Literal["proceed", "reject"] = "proceed"
    appends: list[TranscriptAppend] | None = None
    rewrite: RewriteRequest | None = None
    reason: str | None = None
    tool_choice: str | None = None
    #: Label for the ``hook_action`` event when this action appended context
    #: (a rewrite is labelled by its own ``RewriteRequest.action``). Skill
    #: catalog injection owns ``"skill_catalog"``; the generic default is
    #: ``"append"``.
    append_action: str | None = None


@dataclass(slots=True)
class CompletionDraft:
    """The terminal state the loop is about to emit, offered to
    ``before_completion`` for veto.

    ``content`` is the round's (raw) assistant text — empty when the turn
    ends tool-only or on an error path. ``had_tool_calls`` covers this round;
    ``tool_successes`` covers the whole turn.
    """

    status: str
    reason: str
    content: str
    round_index: int
    had_tool_calls: bool
    tool_calls_used: int
    tool_successes: int


@dataclass(slots=True)
class CompletionAction:
    """Outcome of a ``before_completion`` hook.

    - ``accept`` (default): emit the completion as drafted.
    - ``retry``: append ``message`` to the transcript and run another round
      (tools offered as configured). The anti-hallucination layer uses this
      to send deferred/claimed/fabricated replies back with a discipline
      notice. The hook is responsible for bounding retries.
    - ``narrate``: run one no-tools round seeded with ``message`` so the
      model summarizes what happened, then accept its text as the final
      content. Used when the draft has no natural-language content.
    """

    kind: Literal["accept", "retry", "narrate"] = "accept"
    message: str | None = None
    reason: str | None = None


@dataclass(slots=True)
class RetryAction:
    """Outcome of an ``on_request_error`` hook.

    ``fail`` (default) surfaces the error and ends the turn. ``retry``
    re-issues the request after ``delay_ms`` — with ``rewrite`` when the
    hook declares a transcript replacement (the context-overflow recovery
    compacts before retrying; the loop applies it through the declared
    ``replace_all`` path for the retried request and the rest of the run).
    """

    kind: Literal["fail", "retry"] = "fail"
    delay_ms: int = 0
    reason: str | None = None
    rewrite: RewriteRequest | None = None


# ---------------------------------------------------------------------------
# Protocol + default no-op
# ---------------------------------------------------------------------------


@runtime_checkable
class LoopHooks(Protocol):
    """Extension points called by CoreLoop at fixed positions.

    Implementations must be side-effect-safe to call repeatedly; the loop
    invokes them once per step / tool result / request error respectively.
    """

    async def pre_step(
        self, transcript: list[LLMMessage], ctx: LoopContext
    ) -> PreStepAction: ...

    async def post_tool_result(
        self, result: ToolResult, call: ToolCall, ctx: LoopContext
    ) -> ToolResult: ...

    async def on_request_error(
        self, error: Exception, transcript: list[LLMMessage], ctx: LoopContext
    ) -> RetryAction: ...

    async def before_completion(
        self, draft: CompletionDraft, ctx: LoopContext
    ) -> CompletionAction: ...


class NoopHooks:
    """Default hooks: pass everything through unchanged."""

    async def pre_step(
        self, transcript: list[LLMMessage], ctx: LoopContext
    ) -> PreStepAction:
        return PreStepAction(kind="proceed")

    async def post_tool_result(
        self, result: ToolResult, call: ToolCall, ctx: LoopContext
    ) -> ToolResult:
        return result

    async def on_request_error(
        self, error: Exception, transcript: list[LLMMessage], ctx: LoopContext
    ) -> RetryAction:
        return RetryAction(kind="fail", reason=str(error))

    async def before_completion(
        self, draft: CompletionDraft, ctx: LoopContext
    ) -> CompletionAction:
        return CompletionAction(kind="accept")

    def wrap_up_may_drop_tools(self) -> bool:
        """False keeps offering tools after ``wrap_up_max_tool_rounds``.

        DeliveryHooks returns False while instruction-named required files
        are still missing so wrap-up cannot accept a text-only stop.
        """
        return True


class ChainHooks:
    """Compose several hooks into one ``LoopHooks``.

    - ``pre_step``: applied in order, the working projection threading
      through each so every hook sees the effects of its predecessors. The
      chain merges to one declaration: a rewrite folds earlier appends into
      its message list (the rewriter computed against them), and appends
      after a rewrite fold into that rewrite — "hook1 rewrites, hook2
      appends" is exactly ``replace_all`` then ``append``. The first
      ``reject`` wins; the first non-``None`` ``tool_choice`` wins.
    - ``post_tool_result``: the result threads through each hook in order.
    - ``on_request_error``: the first ``retry`` decision wins; if every hook
      says ``fail``, the first failure reason is surfaced.
    - ``before_completion``: the first non-``accept`` action wins.
    - ``wrap_up_may_drop_tools``: False if any hook forbids dropping tools.

    This is how a product stacks e.g. compaction + spill + retry without the
    loop knowing about any of them.
    """

    def __init__(self, *hooks: LoopHooks) -> None:
        self._hooks = hooks

    async def pre_step(
        self, transcript: list[LLMMessage], ctx: LoopContext
    ) -> PreStepAction:
        current = transcript
        tool_choice: str | None = None
        rewrite: RewriteRequest | None = None
        appends: list[TranscriptAppend] = []
        append_action: str | None = None
        reason: str | None = None
        for hook in self._hooks:
            action = await hook.pre_step(current, ctx)
            if action.kind == "reject":
                return action
            if action.rewrite is not None:
                # The rewriter computed against the current projection, so
                # its message list already contains any earlier appends.
                rewrite = action.rewrite
                appends = []
                current = list(action.rewrite.messages)
            if action.appends:
                current = [*current, *(a.message for a in action.appends)]
                if rewrite is not None:
                    # Fold into the pending rewrite: one replace_all covers
                    # both, and the projection is identical.
                    rewrite = RewriteRequest(
                        messages=current, reason=rewrite.reason, action=rewrite.action
                    )
                else:
                    appends.extend(action.appends)
                if action.append_action is not None:
                    append_action = action.append_action
                if reason is None:
                    reason = action.reason
            if action.rewrite is not None:
                reason = action.rewrite.reason
            if tool_choice is None and action.tool_choice is not None:
                tool_choice = action.tool_choice
        return PreStepAction(
            kind="proceed",
            appends=appends or None,
            rewrite=rewrite,
            reason=reason,
            tool_choice=tool_choice,
            append_action=append_action,
        )

    async def post_tool_result(
        self, result: ToolResult, call: ToolCall, ctx: LoopContext
    ) -> ToolResult:
        current = result
        for hook in self._hooks:
            current = await hook.post_tool_result(current, call, ctx)
        return current

    async def on_request_error(
        self, error: Exception, transcript: list[LLMMessage], ctx: LoopContext
    ) -> RetryAction:
        first_fail: RetryAction | None = None
        for hook in self._hooks:
            action = await hook.on_request_error(error, transcript, ctx)
            if action.kind == "retry":
                return action
            if first_fail is None:
                first_fail = action
        return first_fail or RetryAction(kind="fail", reason=str(error))

    async def before_completion(
        self, draft: CompletionDraft, ctx: LoopContext
    ) -> CompletionAction:
        for hook in self._hooks:
            action = await hook.before_completion(draft, ctx)
            if action.kind != "accept":
                return action
        return CompletionAction(kind="accept")

    def wrap_up_may_drop_tools(self) -> bool:
        for hook in self._hooks:
            drop = getattr(hook, "wrap_up_may_drop_tools", None)
            if callable(drop) and not drop():
                return False
        return True
