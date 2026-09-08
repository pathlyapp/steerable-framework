"""Context compaction — a ``pre_step`` hook.

Local models run on a small context window (deeppath-agent uses 60k); a few
rounds of verbose tool output fill it. Modeled on dsh's ``compaction-basic``:
check pressure before the request is derived, and when over threshold rewrite
the transcript so the turn can continue.

Strategy (deterministic first, LLM optional):

1. Measure context pressure. Ground truth is the provider-reported prompt
   tokens of the previous request (exposed on ``ctx.last_prompt_tokens``)
   plus a heuristic estimate of the messages appended since; only the first
   round falls back to a full heuristic estimate (CJK-aware, per-model
   calibrated — see ``tokens.py``).
2. Under ``threshold_ratio * max_context_tokens`` → proceed unchanged.
3. Over threshold →
   a. fold old ``tool`` messages (beyond ``keep_last_tool_results``) into a
      one-line placeholder keeping a short excerpt — tool output is the
      usual space hog;
   b. if still over and a summarizer provider is configured, replace the
      middle span (after the head, before the recent tail) with a summary
      message; otherwise drop the middle span behind a marker.

System messages and the first user message (the goal) are always kept; the
most recent ``keep_last_messages`` are never touched. Between compactions
the transcript is append-only, so provider prompt caches keep hitting; a
rewrite invalidates the cache once, then the prefix is stable again.

Three trigger paths share the fold/summarize machinery:

- **pressure** (``pre_step``, above) — the reactive default;
- **overflow recovery** (``on_request_error``) — the heuristic threshold can
  still miss the real window, so a context-overflow error forces one
  compaction pass and retries, bounded per round;
- **micro-compaction** (``pre_step``, opt-in ``micro_compact_interval_rounds``)
  — every N rounds, fold old tool results regardless of pressure (CC
  time-based microcompact parity). Off by default: each fold invalidates the
  prompt-cache prefix, so the interval trades cache hits for a bounded
  transcript;
- **manual** (``compact_now``) — the host-command path (CC ``/compact``
  parity): fold + summarize on demand, bypassing threshold, hysteresis, and
  the circuit breaker.

Two safety rails share the machinery:

- Every rewrite carries its ``pre_tokens`` / ``post_tokens`` estimate onto
  the recorded ``CompactionBoundary`` (CC ``compact_boundary`` parity), so
  traces chart compaction effectiveness without re-estimating from bodies.
- A **circuit breaker** (CC auto-compact breaker parity): a pressure
  compaction whose post-estimate is still over threshold counts as
  ineffective; three consecutive ineffective compactions open the circuit
  and the pressure path stops firing — each further rewrite would only
  invalidate the prompt-cache prefix without shrinking the transcript. The
  overflow-recovery path keeps its own per-round bound and stays live, so
  the turn still fails loud (bounded retries, then a named error) instead
  of spinning. A round that lands under threshold — or any successful
  compaction — resets the count.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .hooks import NoopHooks, PreStepAction, RetryAction, RewriteRequest
from .llm import LLMMessage, LLMProvider
from .llm.errors import classify_error
from .tokens import estimate_tokens

_SUMMARY_MARKER = "[context compacted: earlier conversation summarized]"
_FOLDED_TOOL_MARKER = "[tool output folded to save context]"

#: How much of a folded tool result survives as a clue (file paths, error
#: types, key numbers) so the model can still reason about what it saw.
_FOLD_EXCERPT_CHARS = 160

__all__ = ["CompactionHooks", "estimate_tokens"]


def _fold_content(content: str | None, excerpt_chars: int = _FOLD_EXCERPT_CHARS) -> str:
    text = content or ""
    if len(text) <= excerpt_chars:
        excerpt = text
    else:
        head = max(excerpt_chars // 5, 1)
        tail = excerpt_chars - head
        omitted = len(text) - excerpt_chars
        excerpt = f"{text[:head]}\n...[{omitted} chars truncated]...\n{text[-tail:]}"
    if excerpt:
        return f"{_FOLDED_TOOL_MARKER} excerpt: {excerpt}"
    return _FOLDED_TOOL_MARKER


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
        model: str | None = None,
        recompact_margin_ratio: float = 0.1,
        fold_excerpt_chars: int = _FOLD_EXCERPT_CHARS,
        micro_compact_interval_rounds: int = 0,
    ) -> None:
        if not 0 < threshold_ratio <= 1:
            raise ValueError("threshold_ratio must be in (0, 1]")
        if not 0 <= recompact_margin_ratio:
            raise ValueError("recompact_margin_ratio must be >= 0")
        if not 0 <= micro_compact_interval_rounds:
            raise ValueError("micro_compact_interval_rounds must be >= 0")
        self._max_tokens = max_context_tokens
        self._threshold = threshold_ratio
        self._keep_last = keep_last_messages
        self._keep_last_tools = keep_last_tool_results
        self._fold_excerpt_chars = fold_excerpt_chars
        self._summarizer = summarizer
        #: Proactive micro-compaction (CC time-based microcompact parity):
        #: every N rounds, fold old tool results regardless of pressure, so a
        #: long chatty run never drifts toward the window. 0 (default) is off
        #: — each fold invalidates the provider prompt-cache prefix, so the
        #: interval trades cache hits for a bounded transcript.
        self._micro_compact_interval = micro_compact_interval_rounds
        #: Model name used for calibrated token estimates (see tokens.py).
        self._model = model
        #: Hysteresis: after a compaction, pressure must grow by
        #: ``recompact_margin_ratio * max_context_tokens`` before the next one
        #: fires. Without it a transcript that stays over threshold even after
        #: folding+summarizing re-compacts EVERY round — each rewrite destroys
        #: the provider prompt-cache prefix (the dogfood 22-compacts/5-traces
        #: pathology).
        self._recompact_margin = recompact_margin_ratio * max_context_tokens
        self._last_compaction_pressure: int | None = None
        # Observability for callers/tests: how many compactions happened.
        self.compactions = 0
        # Observability: how many of them were periodic micro-compactions.
        self.micro_compactions = 0
        # Overflow-recovery state: consecutive context-overflow retries within
        # one round. Capped so a pathological transcript cannot spin a
        # compact→overflow→compact loop forever.
        self._overflow_round = -1
        self._overflow_attempts = 0
        #: Max forced compactions per round on context-overflow errors.
        self.max_overflow_retries = 2
        # Observability: how many overflow-driven compactions happened.
        self.overflow_recoveries = 0
        #: Consecutive ineffective pressure compactions (post-estimate still
        #: over threshold) before the circuit opens (CC auto-compact breaker
        #: parity). The overflow path is bounded separately and unaffected.
        self.max_consecutive_failures = 3
        self._consecutive_failures = 0
        # Observability: True once the breaker tripped; the pressure path
        # stops firing for the rest of the session.
        self.circuit_open = False

    def _estimate(self, transcript: Sequence[LLMMessage]) -> int:
        return estimate_tokens(transcript, model=self._model)

    def _pressure(self, transcript: Sequence[LLMMessage], ctx: Any) -> int:
        """Estimated size of the NEXT request's prompt.

        The provider-reported prompt tokens of the previous request are
        ground truth for everything sent so far; only the messages appended
        since (assistant turn + tool results + steers) need the heuristic.
        First round (or any state where the observation went stale) falls
        back to a full heuristic estimate.
        """
        observed = getattr(ctx, "last_prompt_tokens", None)
        observed_len = getattr(ctx, "last_prompt_transcript_len", 0) or 0
        if observed is not None and 0 < observed_len <= len(transcript):
            return observed + self._estimate(transcript[observed_len:])
        return self._estimate(transcript)

    @staticmethod
    def _reset_observed(ctx: Any) -> None:
        """A rewrite invalidates the observed indices; the next request
        re-observes. Without this the stale length would mis-slice."""
        if getattr(ctx, "last_prompt_tokens", None) is not None:
            ctx.last_prompt_tokens = None
            ctx.last_prompt_transcript_len = 0

    async def pre_step(
        self, transcript: list[LLMMessage], ctx: Any
    ) -> PreStepAction:
        round_index = getattr(ctx, "round_index", 0)
        if (
            self._micro_compact_interval > 0
            and round_index > 0
            and round_index % self._micro_compact_interval == 0
        ):
            pruned = self._fold_old_tool_results(transcript)
            if pruned is not transcript:
                self.compactions += 1
                self.micro_compactions += 1
                self._reset_observed(ctx)
                return PreStepAction(
                    kind="proceed",
                    rewrite=RewriteRequest(
                        messages=pruned,
                        reason=(
                            "micro-compact: periodic tool-result prune "
                            f"(every {self._micro_compact_interval} rounds)"
                        ),
                        action="micro_compact",
                        pre_tokens=self._estimate(transcript),
                        post_tokens=self._estimate(pruned),
                    ),
                )
        pressure = self._pressure(transcript, ctx)
        if pressure < self._threshold * self._max_tokens:
            # A healthy round resets the breaker count — the pathology (or
            # the transcript that caused it) is gone.
            self._consecutive_failures = 0
            return PreStepAction(kind="proceed")
        if self.circuit_open:
            # Breaker tripped: further pressure rewrites only invalidate the
            # prompt-cache prefix without shrinking the transcript. The
            # overflow path stays live and fails loud if the window is hit.
            return PreStepAction(kind="proceed")
        if (
            self._last_compaction_pressure is not None
            and pressure < self._last_compaction_pressure + self._recompact_margin
        ):
            return PreStepAction(kind="proceed")

        threshold = self._threshold * self._max_tokens
        compacted = self._fold_old_tool_results(transcript)
        if self._estimate(compacted) < threshold:
            self.compactions += 1
            self._consecutive_failures = 0
            self._last_compaction_pressure = pressure
            self._reset_observed(ctx)
            return PreStepAction(
                kind="proceed",
                rewrite=RewriteRequest(
                    messages=compacted,
                    reason="context pressure: folded old tool results",
                    action="compact",
                    pre_tokens=pressure,
                    post_tokens=self._estimate(compacted),
                ),
            )

        compacted = await self._summarize_middle(compacted)
        post = self._estimate(compacted)
        self.compactions += 1
        if post < threshold:
            self._consecutive_failures = 0
        else:
            # Ineffective: even fold+summarize stayed over threshold (e.g. a
            # single kept tool result bigger than the window). Three in a
            # row open the circuit.
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.max_consecutive_failures:
                self.circuit_open = True
        self._last_compaction_pressure = pressure
        self._reset_observed(ctx)
        return PreStepAction(
            kind="proceed",
            rewrite=RewriteRequest(
                messages=compacted,
                reason="context pressure: summarized middle",
                action="compact",
                pre_tokens=pressure,
                post_tokens=post,
            ),
        )

    async def compact_now(self, transcript: list[LLMMessage], ctx: Any) -> PreStepAction:
        """Manual compaction (CC ``/compact`` parity): fold old tool results,
        then summarize the middle, regardless of pressure, hysteresis, or the
        circuit breaker — the user asked for it. Returns a ``proceed`` action
        carrying the rewrite; when neither stage changes anything the action
        carries no rewrite (nothing was worth invalidating the cache for).
        A manual pass resets the breaker count: the user has taken over.
        """
        pre = self._estimate(transcript)
        compacted = self._fold_old_tool_results(transcript)
        compacted = await self._summarize_middle(compacted)
        self._consecutive_failures = 0
        if compacted is transcript or compacted == transcript:
            return PreStepAction(kind="proceed", reason="compact: nothing to fold")
        self.compactions += 1
        self._last_compaction_pressure = pre
        self._reset_observed(ctx)
        return PreStepAction(
            kind="proceed",
            rewrite=RewriteRequest(
                messages=compacted,
                reason="manual compact",
                action="compact",
                pre_tokens=pre,
                post_tokens=self._estimate(compacted),
            ),
        )

    async def on_request_error(
        self, error: Exception, transcript: list[LLMMessage], ctx: Any
    ) -> RetryAction:
        """Context-overflow recovery: the pre_step threshold is a heuristic
        estimate, so a request can still exceed the real window. Retrying the
        identical payload must re-fail — instead force one compaction pass
        (fold old tool results, then summarize the middle) and retry with the
        rewritten transcript. Bounded per round; non-overflow errors are left
        to the retry hooks downstream in the chain.
        """
        if classify_error(error) != "context_overflow":
            return RetryAction(kind="fail", reason=str(error))

        round_index = getattr(ctx, "round_index", 0)
        if round_index != self._overflow_round:
            self._overflow_round = round_index
            self._overflow_attempts = 0
        self._overflow_attempts += 1
        if self._overflow_attempts > self.max_overflow_retries:
            return RetryAction(
                kind="fail",
                reason=(
                    f"context overflow persists after {self.max_overflow_retries} "
                    f"forced compactions: {error}"
                ),
            )

        compacted = self._fold_old_tool_results(transcript)
        compacted = await self._summarize_middle(compacted)
        self.compactions += 1
        self.overflow_recoveries += 1
        self._last_compaction_pressure = self._pressure(transcript, ctx)
        self._reset_observed(ctx)
        return RetryAction(
            kind="retry",
            delay_ms=0,
            reason="context overflow: compacted transcript before retry",
            rewrite=RewriteRequest(
                messages=compacted,
                reason="context overflow: compacted transcript before retry",
                action="overflow_recovery",
                pre_tokens=self._estimate(transcript),
                post_tokens=self._estimate(compacted),
            ),
        )

    # ------------------------------------------------------------------

    def _fold_old_tool_results(self, transcript: list[LLMMessage]) -> list[LLMMessage]:
        # Already-folded results are excluded: re-folding them would stack
        # markers and invalidate the prompt-cache prefix for zero gain, and
        # ``keep_last_tool_results`` should count *readable* results.
        tool_idx = [
            i
            for i, m in enumerate(transcript)
            if m.role == "tool" and not m.content_text.startswith(_FOLDED_TOOL_MARKER)
        ]
        fold_before = len(tool_idx) - self._keep_last_tools
        if fold_before <= 0:
            return transcript
        fold_set = set(tool_idx[:fold_before])
        return [
            LLMMessage.text_of(
                "tool",
                _fold_content(m.content_text, self._fold_excerpt_chars),
                name=m.name,
                tool_call_id=m.tool_call_id,
            )
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
        summary_msg = LLMMessage.text_of("user", f"{_SUMMARY_MARKER}\n{summary}")
        return [*transcript[:head_end], summary_msg, *transcript[tail_start:]]

    async def _summarize(self, middle: list[LLMMessage]) -> str:
        if self._summarizer is not None:
            prompt = [
                LLMMessage.text_of(
                    "system",
                    "Summarize this conversation segment for an agent that "
                    "needs to continue the task. Preserve: the user's goal, "
                    "actions taken, tool outcomes, and any decisions. Be terse.",
                ),
                LLMMessage.text_of(
                    "user",
                    "\n".join(f"[{m.role}] {m.content_text[:2000]}" for m in middle),
                ),
            ]
            # One-off summarization of a transcript that is about to be
            # discarded: never write it into the prompt cache (pi's
            # retention=none rule). The kwarg is consumed by
            # CacheControlProvider; providers without it ignore the key.
            try:
                message, _usage = await self._summarizer.complete(
                    prompt, cache_retention="none"
                )
            except Exception:
                # Transport/protocol errors here run outside the stream
                # retry loop. Falling back keeps wrap-up running so Harbor
                # still scores files instead of a NonZeroAgentExit.
                return self._excerpt_summary(middle)
            text = (message.content_text or "").strip()
            if text:
                return text
        return self._excerpt_summary(middle)

    def _excerpt_summary(self, middle: list[LLMMessage]) -> str:
        # No summarizer, empty model reply, or a failed complete(): keep
        # role + a short excerpt per message so the thread of actions survives.
        lines = []
        for m in middle:
            excerpt = m.content_text.replace("\n", " ")[:200]
            lines.append(f"[{m.role}] {excerpt}")
        return "\n".join(lines)
