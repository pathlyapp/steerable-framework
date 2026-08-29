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

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Iterable, Literal

from .llm import LLMMessage, LLMProvider, LLMStreamChunk, LLMUsage

CacheRetention = Literal["none", "short", "long"]

#: Anthropic's explicit breakpoint API. Other providers have no breakpoint
#: surface (implicit prefix caches) and pass through unchanged.
_EXPLICIT_CACHE_PROVIDERS = {"anthropic", "claude"}


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
        shaped_tools = (
            place_cache_breakpoints(list(tools)) if tools is not None else None
        )
        out["_cache_tail_anchor"] = True
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
