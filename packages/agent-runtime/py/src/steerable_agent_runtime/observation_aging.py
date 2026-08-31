"""Observation aging (W2.1): tool results degrade by tier as they age.

Long-horizon trajectories drown the model in stale observations: a 40 KB
file listing from round 2 is still verbatim in the prompt at round 30,
outweighing the last three rounds of actual work. Aging applies a graded
rule table keyed on (age in rounds, size in tokens):

- **fresh** — younger than ``fresh_rounds`` or smaller than
  ``keep_tokens``: keep verbatim. Recent and cheap observations are the
  model's working set.
- **compress** — mid-age and larger than ``compress_tokens``: keep the
  result envelope's schema keys (``success`` / ``error`` / ``data`` /
  ``message``) and truncate long string values, so the model can still
  reason about *what* the result was (W2.1.3).
- **fold** — at or past ``fold_after_rounds``: replace with a one-line
  stub naming the tool, original size, originating round, and the tool
  call id under which the full record lives in session history (W2.1.4).

Both rewrites go through the declared ``RewriteRequest`` path: the loop
applies them via ``ContextManager.replace_all``, which records a
``CompactionBoundary`` — the append-only record keeps every original
byte, only the visible projection shrinks. That is what makes folding
expandable: the stub's reference resolves against the durable record.

The hook is idempotent across rounds: each call id's applied tier is
tracked, so a compressed result is not re-compressed every round, and the
only later transition is compress → fold.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from .hooks import NoopHooks, PreStepAction, RewriteRequest
from .llm import LLMMessage
from .tokens import estimate_text_tokens

AgingAction = Literal["keep", "compress", "fold"]


@dataclass(frozen=True, slots=True)
class AgingRules:
    """The (age, size) → action rule table. Ages are rounds since the
    result was first seen; sizes are estimated tokens of the message."""

    #: Younger than this many rounds: always keep verbatim.
    fresh_rounds: int = 3
    #: Smaller than this many tokens: always keep (cheap observations are
    #: not worth rewriting).
    keep_tokens: int = 200
    #: This many rounds old or older: fold to a record reference.
    fold_after_rounds: int = 8
    #: Between fresh and fold: compress when larger than this many tokens.
    compress_tokens: int = 1000
    #: Per-string-value character budget inside a compressed payload.
    compress_value_chars: int = 80
    #: List items kept per list inside a compressed payload.
    compress_list_items: int = 3

    def decide(self, *, age_rounds: int, size_tokens: int) -> AgingAction:
        if age_rounds < self.fresh_rounds or size_tokens <= self.keep_tokens:
            return "keep"
        if age_rounds >= self.fold_after_rounds:
            return "fold"
        if size_tokens > self.compress_tokens:
            return "compress"
        return "keep"


class ObservationAgingHooks(NoopHooks):
    """``pre_step`` hook applying the aging table to tool-result messages.

    ``post_tool_result`` records when each call id first appeared;
    ``pre_step`` walks the visible projection and declares a wholesale
    rewrite when at least one result crosses a tier boundary. Rounds with
    no transition return ``proceed`` untouched — no boundary is recorded.
    """

    def __init__(self, rules: AgingRules | None = None) -> None:
        self._rules = rules or AgingRules()
        #: call id → round the result first entered the transcript.
        self._seen: dict[str, int] = {}
        #: call id → last applied non-keep action (``folded`` is terminal).
        self._applied: dict[str, str] = {}

    async def post_tool_result(self, result: Any, call: Any, ctx: Any) -> Any:
        call_id = getattr(call, "id", None)
        if isinstance(call_id, str) and call_id:
            self._seen.setdefault(call_id, getattr(ctx, "round_index", 0))
        return result

    async def pre_step(
        self, transcript: list[LLMMessage], ctx: Any
    ) -> PreStepAction:
        round_index = getattr(ctx, "round_index", 0)
        messages: list[LLMMessage] = []
        transitions = 0
        for message in transcript:
            aged = self._age_one(message, round_index)
            transitions += aged is not message
            messages.append(aged)
        if not transitions:
            return PreStepAction(kind="proceed")
        return PreStepAction(
            kind="proceed",
            rewrite=RewriteRequest(
                messages=messages,
                reason=f"observation aging: {transitions} result(s) changed tier",
                action="observation_aging",
            ),
        )

    def _age_one(self, message: LLMMessage, round_index: int) -> LLMMessage:
        if message.role != "tool" or not message.tool_call_id:
            return message
        call_id = message.tool_call_id
        seen_round = self._seen.get(call_id)
        if seen_round is None:
            # Seeded history (resumed session): age unknown, leave verbatim.
            return message
        applied = self._applied.get(call_id)
        if applied == "folded":
            return message
        action = self._rules.decide(
            age_rounds=round_index - seen_round,
            size_tokens=estimate_text_tokens(message.content_text),
        )
        if action == "keep" or applied == action:
            return message
        if action == "compress":
            content = _compress_payload(message.content_text, self._rules)
        else:
            content = _fold_stub(
                tool=message.name or "tool",
                tokens=estimate_text_tokens(message.content_text),
                seen_round=seen_round,
                call_id=call_id,
            )
        self._applied[call_id] = "compressed" if action == "compress" else "folded"
        return LLMMessage.text_of(
            "tool", content, name=message.name, tool_call_id=call_id
        )


def _compress_payload(text: str, rules: AgingRules) -> str:
    """Keep the envelope's schema keys; truncate long values (W2.1.3).

    The transcript's tool messages serialize ``{"success", "error"?,
    "data"?, "message"?}`` — every top-level key survives so the model can
    still see whether the call failed and what kind of payload it had.
    Unparseable content (not the envelope shape) degrades to head/tail
    truncation.
    """
    try:
        payload = json.loads(text)
    except ValueError:
        return _head_tail(text, rules.compress_value_chars * 8)
    if not isinstance(payload, dict):
        return _head_tail(text, rules.compress_value_chars * 8)
    compressed = {
        key: _compress_value(value, rules) for key, value in payload.items()
    }
    return json.dumps(compressed, ensure_ascii=False)


def _compress_value(value: Any, rules: AgingRules) -> Any:
    if isinstance(value, str):
        if len(value) <= rules.compress_value_chars:
            return value
        return f"{value[: rules.compress_value_chars]}…[+{len(value) - rules.compress_value_chars} chars]"
    if isinstance(value, dict):
        return {key: _compress_value(item, rules) for key, item in value.items()}
    if isinstance(value, list):
        kept = [_compress_value(item, rules) for item in value[: rules.compress_list_items]]
        if len(value) > rules.compress_list_items:
            kept.append(f"…[+{len(value) - rules.compress_list_items} items]")
        return kept
    return value


def _fold_stub(*, tool: str, tokens: int, seen_round: int, call_id: str) -> str:
    """One-line reference to the full record (W2.1.4)."""
    return (
        f"[observation folded: {tool} result, ~{tokens} tokens, "
        f"from round {seen_round}. Full record retained in session history "
        f"under tool call {call_id}.]"
    )


def _head_tail(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    half = max(budget // 2, 1)
    return f"{text[:half]}\n…[{len(text) - 2 * half} chars omitted]…\n{text[-half:]}"
