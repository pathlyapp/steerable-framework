"""Prompt-cache breakpoint emission — the write half of Wave 2's cache work.

Wave 2 shipped the read side (``LLMUsage.cached_prompt_tokens`` /
``cache_creation_tokens`` parsed per provider and surfaced on
``stage_complete``); this module is the write side. The strategy is pi's
three fixed semantic anchors
(``pi/packages/ai/src/api/anthropic-messages.ts``):

- the system prompt,
- the last tool definition,
- the tail of the transcript (the last user message),

plus pi's compaction rule: a one-off summarization request is sent with
caching disabled for that request only (``cache_retention="none"``), so a
transcript that is about to be discarded is never written into the cache.

Only Anthropic has an explicit breakpoint API (``cache_control`` blocks,
max 4 per request, 5m default / 1h where supported). OpenAI-compatible
caches (OpenAI, DeepSeek, Ollama, vLLM) are implicit prefix caches with no
breakpoint surface — for them the wrapper is a pass-through and the win
comes from the prefix stability the rest of the stack already keeps.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Iterable, Literal

from .hooks import NoopHooks, PreStepAction
from .llm import LLMMessage, LLMProvider, LLMStreamChunk, LLMUsage

logger = logging.getLogger(__name__)

CacheRetention = Literal["none", "short", "long"]

#: Anthropic's explicit breakpoint API. Other providers have no breakpoint
#: surface (implicit prefix caches) and pass through unchanged.
_EXPLICIT_CACHE_PROVIDERS = {"anthropic", "claude"}


def _marker_for(retention: CacheRetention) -> dict[str, Any]:
    """Map a retention class onto the Anthropic breakpoint marker.

    ``short`` is the 5-minute default; ``long`` opts into the 1-hour TTL
    (CC ``CLAUDE_CODE_PROMPT_CACHE_TTL`` parity — a deliberate trade: fewer
    cache writes on slow-moving prefixes, at Anthropic's higher 1h write
    price). ``none`` never reaches here (the request skips anchoring).
    """
    if retention == "long":
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}


@dataclass(slots=True)
class CacheControlProvider:
    """LLMProvider decorator: emits prompt-cache breakpoints per request.

    Placement is computed fresh on every call from the actual request — the
    transcript tail moves every round, so the tail anchor must move with it.
    ``cache_retention="none"`` suppresses every anchor for that one call
    (the compaction summarization case); nothing is persisted across calls.
    """

    inner: LLMProvider
    retention: CacheRetention = "short"

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def model(self) -> str:
        return self.inner.model

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> tuple[LLMMessage, LLMUsage]:
        tools, kwargs = self._apply(tools, kwargs)
        return await self.inner.complete(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    def stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        tools, kwargs = self._apply(tools, kwargs)
        return self.inner.stream(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    # ------------------------------------------------------------------

    def _apply(
        self,
        tools: Iterable[dict[str, Any]] | None,
        kwargs: dict[str, Any],
    ) -> tuple[Iterable[dict[str, Any]] | None, dict[str, Any]]:
        retention = self._effective_retention(kwargs)
        if retention == "none" or self._provider_key() not in _EXPLICIT_CACHE_PROVIDERS:
            kwargs.pop("cache_retention", None)
            return tools, kwargs
        out = dict(kwargs)
        marker = _marker_for(retention)
        shaped_tools = (
            place_cache_breakpoints(list(tools), cache_control=marker)
            if tools is not None
            else None
        )
        # The provider-owned anchors (system block, transcript tail) read the
        # marker off this key so all three breakpoints share one TTL.
        out["_cache_tail_anchor"] = marker
        return shaped_tools, out

    def _effective_retention(self, kwargs: dict[str, Any]) -> CacheRetention:
        per_request = kwargs.get("cache_retention")
        if per_request in ("none", "short", "long"):
            return per_request
        return self.retention

    def _provider_key(self) -> str:
        return (self.inner.name or "").strip().lower()


def place_cache_breakpoints(
    tools: list[dict[str, Any]],
    *,
    cache_control: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Stamp ``cache_control`` on the LAST tool definition (pi's anchor 2).

    Pure and exported for direct unit tests; providers call it on their
    already provider-shaped tool list. Returns a new list — the caller's
    descriptors are never mutated.
    """
    if not tools:
        return tools
    marker = cache_control or {"type": "ephemeral"}
    out = [dict(t) for t in tools]
    out[-1] = {**out[-1], "cache_control": marker}
    return out


def system_blocks_with_cache(
    system_text: str,
    *,
    cache_control: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The system prompt as a block array with a breakpoint (anchor 1).

    Anthropic caches per content block; a bare-string system prompt cannot
    carry a breakpoint, so anchoring requires the block form.
    """
    marker = cache_control or {"type": "ephemeral"}
    return [{"type": "text", "text": system_text, "cache_control": marker}]


class CacheDriftMonitor(NoopHooks):
    """``pre_step`` observer: flag prompt-cache drift from provider usage.

    Each round it reads the PREVIOUS request's accounting off the loop ctx
    (``last_prompt_tokens`` / ``last_cached_prompt_tokens`` — the read side
    surfaced on ``stage_complete``). The monitor arms once a round shows the
    cache actually serving tokens; from then on, ``consecutive_rounds``
    rounds whose hit rate stays below ``min_hit_rate`` (while the prompt is
    at least ``min_prompt_tokens`` — below that, caching barely matters) set
    ``drift_detected`` and log a warning with the numbers. A round at or
    above the rate resets the count and clears the flag.

    This is the deliberate loop logic behind the raw ``stage_complete``
    numbers (CC ``globalCacheStrategy`` / ``cacheControlHash`` parity): a
    collapse means the cached prefix broke — a transcript rewrite, a changed
    tool list, a TTL expiry — and one low round is normal right after a
    compaction (the prefix re-warms on the next request), which is why the
    verdict requires consecutive low rounds. Providers without cache
    accounting never arm the monitor, so implicit-cache deployments that
    report nothing see no false drift.
    """

    def __init__(
        self,
        *,
        min_prompt_tokens: int = 1024,
        min_hit_rate: float = 0.5,
        consecutive_rounds: int = 3,
    ) -> None:
        if not 0 < min_hit_rate <= 1:
            raise ValueError("min_hit_rate must be in (0, 1]")
        if not 1 <= consecutive_rounds:
            raise ValueError("consecutive_rounds must be >= 1")
        self._min_prompt = min_prompt_tokens
        self._min_hit_rate = min_hit_rate
        self._consecutive_rounds = consecutive_rounds
        # Observability for callers/tests (CompactionHooks-counter pattern).
        self.armed = False
        self.drift_detected = False
        self.consecutive_low_hit_rounds = 0
        self.last_hit_rate: float | None = None

    async def pre_step(
        self, transcript: list[LLMMessage], ctx: Any
    ) -> PreStepAction:
        prompt = getattr(ctx, "last_prompt_tokens", None)
        cached = getattr(ctx, "last_cached_prompt_tokens", None)
        if not prompt or cached is None or prompt < self._min_prompt:
            return PreStepAction(kind="proceed")
        hit_rate = cached / prompt
        self.last_hit_rate = hit_rate
        if cached > 0:
            self.armed = True
        if not self.armed:
            return PreStepAction(kind="proceed")
        if hit_rate >= self._min_hit_rate:
            self.consecutive_low_hit_rounds = 0
            self.drift_detected = False
            return PreStepAction(kind="proceed")
        self.consecutive_low_hit_rounds += 1
        if (
            self.consecutive_low_hit_rounds >= self._consecutive_rounds
            and not self.drift_detected
        ):
            self.drift_detected = True
            logger.warning(
                "prompt-cache drift: hit rate %.2f below %.2f for %d "
                "consecutive rounds (prompt=%d cached=%d) — the cached "
                "prefix broke (rewrite, tool-list change, or TTL expiry)",
                hit_rate,
                self._min_hit_rate,
                self.consecutive_low_hit_rounds,
                prompt,
                cached,
            )
        return PreStepAction(kind="proceed")
