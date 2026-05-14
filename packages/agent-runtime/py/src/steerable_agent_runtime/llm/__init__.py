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
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from steerable_agent_protocol.generated import ToolCall

LLMRole = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class LLMMessage:
    """A single chat-message item passed to an LLMProvider."""

    role: LLMRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None


@dataclass(slots=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


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


from .openai_compat import OpenAICompatProvider  # noqa: E402
from .anthropic_native import AnthropicProvider  # noqa: E402

__all__ = [
    "LLMMessage",
    "LLMProvider",
    "LLMRole",
    "LLMStreamChunk",
    "LLMUsage",
    "OpenAICompatProvider",
    "AnthropicProvider",
]
