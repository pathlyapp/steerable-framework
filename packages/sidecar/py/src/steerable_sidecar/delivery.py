"""Headless delivery discipline: stop inspect-only loops and force artifacts.

Overnight Terminal-Bench remainder failed several scored tasks with hidden
pytest `FileNotFoundError` on the named output (`eval.scm`, `program.py`,
`re.json`, …) after tens of bash/read_file calls and zero writes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime.hooks import (
    CompletionAction,
    CompletionDraft,
    NoopHooks,
    PreStepAction,
    TranscriptAppend,
)
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.loop import LoopContext

_MUTATING = frozenset({"write_file", "edit_file"})
_EXPLORE = frozenset({"bash", "read_file"})
# TB agents usually create scored files with bash (`python … open(…,'w')`,
# `cat > /app/out`). Counting only write_file caused a false no_artifact
# retry after the file already existed (count-dataset-tokens).
_BASH_WRITES = re.compile(
    r"(?:>>|(?<![12])>)\s*(?:/|\./|[A-Za-z0-9._-]+/[A-Za-z0-9._/-]*|[A-Za-z0-9._-]+\.[A-Za-z0-9]+)"
    r"|\btee\b"
    r"|\b(?:cp|mv|touch)\s+\S+"
    r"|open\([^)]*['\"][wa]"
    r"|Path\([^)]*\)\.write"
    r"|\.write_text\("
    r"|\b(?:python3?|pypy3?)\s+\S+\.py\b"
    r"|\b(?:g?cc|g\+\+|clang\+\+|rustc)\s+[^\n]*\s-o\s"
    r"|\b(?:make|cmake|ffmpeg|qemu-img|qemu-system)\b"
)

_EXPLORE_NUDGE = (
    "You have inspected the workspace for many steps without creating the "
    "required output files. If you already know contents that satisfy every "
    "constraint the instruction states (path, length, format, metric), write "
    "those files now with write_file, edit_file, or bash. Do not write "
    "placeholders, decoys, guesses, or a prose description of a rendering."
)
_NO_ARTIFACT_RETRY = (
    "The turn is ending without a write to the named output files. Hidden "
    "tests look for those paths. If you already drafted the contents in "
    "this chat, write_file them now; do not only describe a plan or dump "
    "a placeholder."
)
_MISSING_NAMED_RETRY = (
    "The turn is ending but these instruction-named output files still "
    "do not exist: {paths}. Hidden tests look for those paths. Write them "
    "now with write_file, edit_file, or bash — helper scripts alone are "
    "not enough."
)
# Absolute paths TB instructions name as outputs (`/app/re.json`,
# `/tmp/frame.bmp`). Existing paths at start are inputs, not outputs.
_NAMED_OUTPUT_PATH = re.compile(
    r"(?:^|[\s`'\"(\[])"
    r"((?:/app|/tmp|/workspace|/home/agent)"
    r"/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*\.[A-Za-z0-9]+)"
)
_EMPTY_ROUND_RETRY = (
    "You produced no tool call and no final answer (reasoning only). "
    "Continue the task now with bash, read_file, write_file, or edit_file. "
    "Do not stop until the required output files exist."
)


class DeliveryHooks(NoopHooks):
    """Nudge, then veto completion, when a coding turn never mutates files."""

    def __init__(
        self,
        *,
        explore_before_nudge: int = 20,
        max_nudges: int = 3,
        min_tools_for_completion_retry: int = 2,
        max_empty_round_retries: int = 6,
        instruction: str = "",
        named_outputs: Iterable[str] | None = None,
    ) -> None:
        self._explore_before_nudge = explore_before_nudge
        self._max_nudges = max_nudges
        self._min_tools_for_completion_retry = min_tools_for_completion_retry
        self._max_empty_round_retries = max_empty_round_retries
        raw = (
            tuple(named_outputs)
            if named_outputs is not None
            else named_output_paths(instruction)
        )
        self._required = tuple(p for p in raw if not Path(p).exists())
        self.writes = 0
        self.consecutive_explore = 0
        self.nudges = 0
        self.completion_retries = 0
        self.empty_round_retries = 0
        self._force_tool = False

    async def pre_step(
        self, transcript: list[LLMMessage], ctx: LoopContext
    ) -> PreStepAction:
        appends = None
        append_action = None
        reason = None
        if (
            self.writes == 0
            and self.consecutive_explore >= self._explore_before_nudge
            and self.nudges < self._max_nudges
        ):
            self.nudges += 1
            self.consecutive_explore = 0
            appends = [
                TranscriptAppend(
                    message=LLMMessage.text_of("user", _EXPLORE_NUDGE),
                    kind="delivery.explore_nudge",
                )
            ]
            reason = "explore_without_write"
            append_action = "delivery_nudge"
        tool_choice = (
            "required"
            if self._force_tool or ctx.round_index == 0
            else None
        )
        if tool_choice and reason is None:
            reason = (
                "empty_round_force_tool"
                if self._force_tool
                else "first_round_force_tool"
            )
        if appends or tool_choice:
            return PreStepAction(
                kind="proceed",
                appends=appends,
                reason=reason,
                tool_choice=tool_choice,
                append_action=append_action,
            )
        return PreStepAction(kind="proceed")

    async def post_tool_result(
        self, result: ToolResult, call: ToolCall, ctx: LoopContext
    ) -> ToolResult:
        self._force_tool = False
        name = call.name
        if name in _MUTATING or (name == "bash" and _bash_writes(call)):
            self.writes += 1
            self.consecutive_explore = 0
        elif name in _EXPLORE:
            self.consecutive_explore += 1
        return result

    async def before_completion(
        self, draft: CompletionDraft, ctx: LoopContext
    ) -> CompletionAction:
        empty = not (draft.content or "").strip() and not draft.had_tool_calls
        if empty and self.empty_round_retries < self._max_empty_round_retries:
            self.empty_round_retries += 1
            self._force_tool = True
            return CompletionAction(
                kind="retry",
                message=_EMPTY_ROUND_RETRY,
                reason="empty_round",
            )
        missing = tuple(p for p in self._required if not Path(p).exists())
        if missing and self.completion_retries < 1:
            self.completion_retries += 1
            self._force_tool = True
            listed = ", ".join(missing[:8])
            return CompletionAction(
                kind="retry",
                message=_MISSING_NAMED_RETRY.format(paths=listed),
                reason="missing_named_output",
            )
        if (
            not self._required
            and self.writes == 0
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


def named_output_paths(instruction: str) -> tuple[str, ...]:
    """Absolute output paths named in a TB instruction (not `/usr` inputs)."""
    seen: list[str] = []
    for match in _NAMED_OUTPUT_PATH.finditer(instruction or ""):
        path = match.group(1)
        if path not in seen:
            seen.append(path)
    return tuple(seen)


def _bash_writes(call: ToolCall) -> bool:
    args = call.arguments or {}
    command = str(args.get("command") or args.get("cmd") or args.get("script") or "")
    return bool(_BASH_WRITES.search(command))
