"""Context compaction — a ``pre_step`` hook.

Local models run on a small context window (deeppath-agent uses 60k); a few
rounds of verbose tool output fill it. Modeled on dsh's ``compaction-basic``:
check pressure before the request is derived, and when over threshold rewrite
the transcript so the turn can continue.

Strategy (deterministic first, LLM optional):

1. Estimate transcript tokens (chars/4 heuristic — good enough for a pressure
   trigger; providers report real usage for the budget axis).
2. Under ``threshold_ratio * max_context_tokens`` → proceed unchanged.
3. Over threshold →
   a. fold old ``tool`` messages (beyond ``keep_last_tool_results``) into a
      one-line placeholder — tool output is the usual space hog;
   b. if still over and a summarizer provider is configured, replace the
      middle span (after the head, before the recent tail) with a summary
      message; otherwise drop the middle span behind a marker.

System messages and the first user message (the goal) are always kept; the
most recent ``keep_last_messages`` are never touched.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .hooks import NoopHooks, PreStepAction
from .llm import LLMMessage, LLMProvider

_SUMMARY_MARKER = "[context compacted: earlier conversation summarized]"
_FOLDED_TOOL_MARKER = "[tool output folded to save context]"


def estimate_tokens(messages: Sequence[LLMMessage]) -> int:
    """Rough token estimate: ~4 chars per token, plus a per-message overhead."""
    total = 0
    for m in messages:
        total += 4  # role / framing overhead
        total += len(m.content or "") // 4
        if m.tool_calls:
            for call in m.tool_calls:
                total += len(call.name) // 4 + len(str(call.arguments)) // 4 + 8
    return total


class CompactionHooks(NoopHooks):
    """``pre_step`` hook: compact the transcript when context pressure is high."""

    def __init__(
        self,
        *,
        max_context_tokens: int,
        threshold_ratio: float = 0.8,
        keep_last_messages: int = 6,
        keep_last_tool_results: int = 2,
        summarizer: LLMProvider | None = None,
    ) -> None:
        if not 0 < threshold_ratio <= 1:
            raise ValueError("threshold_ratio must be in (0, 1]")
        self._max_tokens = max_context_tokens
        self._threshold = threshold_ratio
        self._keep_last = keep_last_messages
        self._keep_last_tools = keep_last_tool_results
        self._summarizer = summarizer
        # Observability for callers/tests: how many compactions happened.
        self.compactions = 0

    async def pre_step(
        self, transcript: list[LLMMessage], ctx: Any
    ) -> PreStepAction:
        if estimate_tokens(transcript) < self._threshold * self._max_tokens:
            return PreStepAction(kind="proceed", transcript=transcript)

        compacted = self._fold_old_tool_results(transcript)
        if estimate_tokens(compacted) < self._threshold * self._max_tokens:
            self.compactions += 1
            return PreStepAction(kind="proceed", transcript=compacted)

        compacted = await self._summarize_middle(compacted)
        self.compactions += 1
        return PreStepAction(kind="proceed", transcript=compacted)

    # ------------------------------------------------------------------

    def _fold_old_tool_results(self, transcript: list[LLMMessage]) -> list[LLMMessage]:
        tool_idx = [i for i, m in enumerate(transcript) if m.role == "tool"]
        fold_before = len(tool_idx) - self._keep_last_tools
        if fold_before <= 0:
            return transcript
        fold_set = set(tool_idx[:fold_before])
        return [
            LLMMessage(role="tool", name=m.name, tool_call_id=m.tool_call_id, content=_FOLDED_TOOL_MARKER)
            if i in fold_set
            else m
            for i, m in enumerate(transcript)
        ]

    async def _summarize_middle(self, transcript: list[LLMMessage]) -> list[LLMMessage]:
        # Keep every system message plus the first user message (the goal) as
        # the head; keep the recent tail untouched; summarize the middle.
        head_end = 0
        for i, m in enumerate(transcript):
            if m.role == "system" or (m.role == "user" and not any(
                x.role == "user" for x in transcript[:i]
            )):
                head_end = i + 1
            else:
                break
        tail_start = max(head_end, len(transcript) - self._keep_last)
        middle = transcript[head_end:tail_start]
        if not middle:
            return transcript

        summary = await self._summarize(middle)
        summary_msg = LLMMessage(role="user", content=f"{_SUMMARY_MARKER}\n{summary}")
        return [*transcript[:head_end], summary_msg, *transcript[tail_start:]]

    async def _summarize(self, middle: list[LLMMessage]) -> str:
        if self._summarizer is not None:
            prompt = [
                LLMMessage(
                    role="system",
                    content=(
                        "Summarize this conversation segment for an agent that "
                        "needs to continue the task. Preserve: the user's goal, "
                        "actions taken, tool outcomes, and any decisions. Be terse."
                    ),
                ),
                LLMMessage(
                    role="user",
                    content="\n".join(
                        f"[{m.role}] {(m.content or '')[:2000]}" for m in middle
                    ),
                ),
            ]
            message, _usage = await self._summarizer.complete(prompt)
            return message.content
        # No summarizer configured: deterministic fallback keeps role + a short
        # excerpt per message so the thread of actions survives.
        lines = []
        for m in middle:
            excerpt = (m.content or "").replace("\n", " ")[:200]
            lines.append(f"[{m.role}] {excerpt}")
        return "\n".join(lines)
