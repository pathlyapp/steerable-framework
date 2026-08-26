"""Loop extension points — the seam that keeps CoreLoop small.

dsh hangs compaction / retry / approval off ``agent/pre-step`` and
``agent/request-error`` plugins so adding a capability never edits the loop.
CoreLoop takes the same shape with three hook points; each remaining A3 slice
(compaction, large-result spill, retry, …) lands as a hooks implementation
instead of more branches in ``loop.py``.

Hook points and their intended consumers:

- ``pre_step`` — before each LLM stream. Compaction rewrites the transcript
  here; a hook may also reject the step (turn ends blocked).
- ``post_tool_result`` — after each tool execution, before the result enters
  the transcript. Large-result externalization rewrites oversized results to
  a preview + locator here.
- ``on_request_error`` — when the LLM stream raises. Retry policy decides
  retry-with-backoff vs fail here.

All hooks default to no-op (``NoopHooks``), so wiring them in changes no
existing behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from steerable_agent_protocol.generated import ToolCall, ToolResult

if TYPE_CHECKING:
    from .llm import LLMMessage
    from .loop import LoopContext

# ---------------------------------------------------------------------------
# Action types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PreStepAction:
    """Outcome of a ``pre_step`` hook.

    ``proceed`` (default) continues with ``transcript`` (possibly rewritten,
    e.g. compacted). ``reject`` ends the turn without calling the model.

    ``tool_choice`` (e.g. ``"required"``) is forwarded to the provider for
    this step's LLM call — the data-need router uses it to force a tool call
    on the first round of a data-seeking turn. Pass-through value, forwarded
    as a provider kwarg; providers that cannot honor it ignore it.
    """

    kind: Literal["proceed", "reject"] = "proceed"
    transcript: list[LLMMessage] | None = None
    reason: str | None = None
    tool_choice: str | None = None


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
    re-issues the request after ``delay_ms``.
    """

    kind: Literal["fail", "retry"] = "fail"
    delay_ms: int = 0
    reason: str | None = None


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
        self, error: Exception, ctx: LoopContext
    ) -> RetryAction: ...

    async def before_completion(
        self, draft: CompletionDraft, ctx: LoopContext
    ) -> CompletionAction: ...


class NoopHooks:
    """Default hooks: pass everything through unchanged."""

    async def pre_step(
        self, transcript: list[LLMMessage], ctx: LoopContext
    ) -> PreStepAction:
        return PreStepAction(kind="proceed", transcript=transcript)

    async def post_tool_result(
        self, result: ToolResult, call: ToolCall, ctx: LoopContext
    ) -> ToolResult:
        return result

    async def on_request_error(
        self, error: Exception, ctx: LoopContext
    ) -> RetryAction:
        return RetryAction(kind="fail", reason=str(error))

    async def before_completion(
        self, draft: CompletionDraft, ctx: LoopContext
    ) -> CompletionAction:
        return CompletionAction(kind="accept")


class ChainHooks:
    """Compose several hooks into one ``LoopHooks``.

    - ``pre_step``: applied in order, the transcript threading through each;
      the first ``reject`` wins; the first non-``None`` ``tool_choice`` wins.
    - ``post_tool_result``: the result threads through each hook in order.
    - ``on_request_error``: the first ``retry`` decision wins; if every hook
      says ``fail``, the first failure reason is surfaced.
    - ``before_completion``: the first non-``accept`` action wins.

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
        for hook in self._hooks:
            action = await hook.pre_step(current, ctx)
            if action.kind == "reject":
                return action
            if action.transcript is not None:
                current = action.transcript
            if tool_choice is None and action.tool_choice is not None:
                tool_choice = action.tool_choice
        return PreStepAction(
            kind="proceed", transcript=current, tool_choice=tool_choice
        )

    async def post_tool_result(
        self, result: ToolResult, call: ToolCall, ctx: LoopContext
    ) -> ToolResult:
        current = result
        for hook in self._hooks:
            current = await hook.post_tool_result(current, call, ctx)
        return current

    async def on_request_error(
        self, error: Exception, ctx: LoopContext
    ) -> RetryAction:
        first_fail: RetryAction | None = None
        for hook in self._hooks:
            action = await hook.on_request_error(error, ctx)
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
