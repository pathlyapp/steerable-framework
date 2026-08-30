"""LLMProvider interface and reference implementations.

The runtime intentionally keeps LLMProvider small. Higher-level concerns
(retry, budget, multi-step orchestration) live in `steerable_agent_harness`.

The interface speaks the protocol-level types (`ToolCall`, `ToolResult`,
`ChatMessage`) but accepts a slightly looser `LLMMessage` shape for inputs so
callers do not have to materialise full ChatMessage records when constructing
prompts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from steerable_agent_protocol.generated import ToolCall

from .parts import ContentPart, ImagePart, TextPart, content_text, text_parts

LLMRole = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class LLMMessage:
    """A single chat-message item passed to an LLMProvider.

    ``content`` is a list of typed parts (Wave 1). Text-only messages — the
    common case — are built with ``text_of`` and read back with
    ``content_text``; providers serialize them in the legacy string
    shorthand, so text-only wire bytes are unchanged.
    """

    role: LLMRole
    content: list[ContentPart]
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None

    @classmethod
    def text_of(
        cls,
        role: LLMRole,
        text: str,
        *,
        name: str | None = None,
        tool_call_id: str | None = None,
        tool_calls: list[ToolCall] | None = None,
    ) -> LLMMessage:
        """Build a text-only message (single ``TextPart``)."""
        return cls(
            role=role,
            content=text_parts(text),
            name=name,
            tool_call_id=tool_call_id,
            tool_calls=tool_calls,
        )

    @property
    def content_text(self) -> str:
        """Plain-text projection of the content parts (images elided)."""
        return content_text(self.content)


@dataclass(slots=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    #: Prompt tokens served from the provider's prompt cache — OpenAI
    #: ``prompt_tokens_details.cached_tokens``, DeepSeek
    #: ``prompt_cache_hit_tokens``, Anthropic ``cache_read_input_tokens``.
    #: Zero when the provider does not report cache accounting.
    cached_prompt_tokens: int = 0
    #: Tokens written into the provider's cache by this request — Anthropic
    #: ``cache_creation_input_tokens``. OpenAI-compatible caches are
    #: implicit and report no creation accounting, so this stays zero there.
    cache_creation_tokens: int = 0


@dataclass(slots=True)
class LLMStreamChunk:
    """Provider-agnostic stream chunk."""

    content_delta: str | None = None
    reasoning_delta: str | None = None
    tool_call_delta: ToolCall | None = None
    finish_reason: str | None = None
    usage: LLMUsage | None = None
    raw: Any | None = None


@runtime_checkable
class LLMProvider(Protocol):
    """Async chat-completion adapter.

    Implementations must support both `complete()` (one-shot) and `stream()`
    (incremental). Both flavors must:
      * Accept a sequence of LLMMessage records.
      * Optionally accept a list of tool descriptors (already in OpenAI
        function-calling shape; providers that need a different shape transform
        internally).
      * Return / yield content alongside any tool calls the model proposed.
      * Surface usage tokens whenever the upstream provider reports them.
    """

    name: str
    model: str

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> tuple[LLMMessage, LLMUsage]:
        ...

    def stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        ...


from .anthropic_native import AnthropicProvider
from .compat import (
    PROVIDER_COMPAT_HOSTS,
    OpenAICompatFlags,
    compat_for_base_url,
)
from .errors import (
    RETRYABLE_KINDS,
    LLMError,
    LLMErrorKind,
    classify_error,
    classify_http_status,
    is_retryable,
)
from .openai_compat import OpenAICompatProvider

__all__ = [
    "PROVIDER_COMPAT_HOSTS",
    "RETRYABLE_KINDS",
    "AnthropicProvider",
    "ContentPart",
    "ImagePart",
    "LLMError",
    "LLMErrorKind",
    "LLMMessage",
    "LLMProvider",
    "LLMRole",
    "LLMStreamChunk",
    "LLMUsage",
    "OpenAICompatFlags",
    "OpenAICompatProvider",
    "TextPart",
    "classify_error",
    "classify_http_status",
    "compat_for_base_url",
    "content_text",
    "is_retryable",
    "text_parts",
]
