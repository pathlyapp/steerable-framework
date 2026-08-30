"""Headless delivery discipline: stop inspect-only loops and force artifacts.

Overnight Terminal-Bench remainder failed several scored tasks with hidden
pytest `FileNotFoundError` on the named output (`eval.scm`, `program.py`,
`re.json`, …) after tens of bash/read_file calls and zero writes.
"""

from __future__ import annotations

from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime.hooks import (
    CompletionAction,
    CompletionDraft,
    PreStepAction,
    RetryAction,
    TranscriptAppend,
)
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.loop import LoopContext

_MUTATING = frozenset({"write_file", "edit_file"})
_EXPLORE = frozenset({"bash", "read_file"})

_EXPLORE_NUDGE = (
    "You have inspected the workspace but not written the required output "
    "files. Stop reading sources. Create those files now with write_file or "
    "edit_file, then verify with bash."
)
_NO_ARTIFACT_RETRY = (
    "The turn is ending without write_file or edit_file. Hidden tests look "
    "for named output files. Write them now; do not only describe the plan."
)


class DeliveryHooks:
    """Nudge, then veto completion, when a coding turn never mutates files."""

    def __init__(
        self,
        *,
        explore_before_nudge: int = 8,
        max_nudges: int = 2,
        min_tools_for_completion_retry: int = 2,
    ) -> None:
        self._explore_before_nudge = explore_before_nudge
        self._max_nudges = max_nudges
        self._min_tools_for_completion_retry = min_tools_for_completion_retry
        self.writes = 0
        self.consecutive_explore = 0
        self.nudges = 0
        self.completion_retries = 0

    async def pre_step(
        self, transcript: list[LLMMessage], ctx: LoopContext
    ) -> PreStepAction:
        if (
            self.writes == 0
            and self.consecutive_explore >= self._explore_before_nudge
            and self.nudges < self._max_nudges
        ):
            self.nudges += 1
            self.consecutive_explore = 0
            return PreStepAction(
                kind="proceed",
                appends=[
                    TranscriptAppend(
                        message=LLMMessage.text_of("user", _EXPLORE_NUDGE),
                        kind="delivery.explore_nudge",
                    )
                ],
                reason="explore_without_write",
                append_action="delivery_nudge",
            )
        return PreStepAction(kind="proceed")

    async def post_tool_result(
        self, result: ToolResult, call: ToolCall, ctx: LoopContext
    ) -> ToolResult:
        name = call.name
        if name in _MUTATING:
            self.writes += 1
            self.consecutive_explore = 0
        elif name in _EXPLORE:
            self.consecutive_explore += 1
        return result

    async def on_request_error(
        self, error: Exception, transcript: list[LLMMessage], ctx: LoopContext
    ) -> RetryAction:
        return RetryAction(kind="fail", reason=str(error))

    async def before_completion(
        self, draft: CompletionDraft, ctx: LoopContext
    ) -> CompletionAction:
        if (
            self.writes == 0
            and draft.tool_calls_used >= self._min_tools_for_completion_retry
            and self.completion_retries < 1
        ):
            self.completion_retries += 1
            return CompletionAction(
                kind="retry",
                message=_NO_ARTIFACT_RETRY,
                reason="no_artifact",
            )
        return CompletionAction(kind="accept")
