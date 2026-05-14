"""Anthropic native-protocol provider.

Why a separate provider instead of always using OpenAI-compatible endpoints?
Some hosted gateways aggressively buffer the OpenAI-compatible SSE stream and
break true byte-level streaming. Going directly to the Anthropic native
``/v1/messages`` endpoint (or its vendor mirror) produces sub-second first-byte
latency under those gateways.

This implementation is intentionally a thin shim around the official
``anthropic`` Python SDK so we inherit auth, retries, and SSE parsing.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from steerable_agent_protocol.generated import ToolCall

from . import LLMMessage, LLMStreamChunk, LLMUsage


@dataclass(slots=True)
class AnthropicProvider:
    name: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    default_temperature: float | None = None
    default_max_tokens: int = 1024

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> tuple[LLMMessage, LLMUsage]:
        client = self._client()
        body = self._build_body(
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            extra=kwargs,
        )
        message = await client.messages.create(**body)
        text_chunks: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in message.content or []:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_chunks.append(getattr(block, "text", "") or "")
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=getattr(block, "id", "") or "",
                        name=getattr(block, "name", "") or "",
                        arguments=getattr(block, "input", {}) or {},
                    )
                )
        usage = getattr(message, "usage", None)
        out_usage = LLMUsage(
            prompt_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            total_tokens=int(
                (getattr(usage, "input_tokens", 0) or 0)
                + (getattr(usage, "output_tokens", 0) or 0)
            ),
        )
        return (
            LLMMessage(
                role="assistant",
                content="".join(text_chunks),
                tool_calls=tool_calls or None,
            ),
            out_usage,
        )

    async def stream(  # type: ignore[override]
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        client = self._client()
        body = self._build_body(
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            extra=kwargs,
        )
        async with client.messages.stream(**body) as stream:
            async for event in stream:
                chunk = _parse_anthropic_event(event)
                if chunk is not None:
                    yield chunk
            final = await stream.get_final_message()
            usage = getattr(final, "usage", None)
            if usage is not None:
                yield LLMStreamChunk(
                    usage=LLMUsage(
                        prompt_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                        completion_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                        total_tokens=int(
                            (getattr(usage, "input_tokens", 0) or 0)
                            + (getattr(usage, "output_tokens", 0) or 0)
                        ),
                    ),
                    finish_reason="stop",
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _client(self) -> Any:
        from anthropic import AsyncAnthropic  # local import keeps optional dep optional

        kwargs: dict[str, Any] = {}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return AsyncAnthropic(**kwargs)

    def _build_body(
        self,
        *,
        messages: Sequence[LLMMessage],
        tools: Iterable[dict[str, Any]] | None,
        temperature: float | None,
        max_tokens: int | None,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        system_text, formatted = _split_system_and_messages(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": formatted,
            "max_tokens": max_tokens or self.default_max_tokens,
        }
        if system_text:
            body["system"] = system_text
        eff_temperature = temperature if temperature is not None else self.default_temperature
        if eff_temperature is not None:
            body["temperature"] = eff_temperature
        if tools:
            anth_tools = [_openai_tool_to_anthropic(t) for t in tools if isinstance(t, dict)]
            if anth_tools:
                body["tools"] = anth_tools
        body.update(extra)
        return body


# ---------------------------------------------------------------------------
# Helpers (pure functions kept module-level for unit-testability)
# ---------------------------------------------------------------------------


def _split_system_and_messages(
    messages: Sequence[LLMMessage],
) -> tuple[str | None, list[dict[str, Any]]]:
    system_chunks: list[str] = []
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            if message.content:
                system_chunks.append(message.content)
            continue
        if message.role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.tool_call_id or "",
                            "content": message.content,
                        }
                    ],
                }
            )
            continue
        if message.tool_calls:
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for tc in message.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.id or "",
                        "name": tc.name,
                        "input": tc.arguments or {},
                    }
                )
            out.append({"role": "assistant", "content": blocks})
            continue
        out.append({"role": message.role, "content": message.content})
    system = "\n\n".join(system_chunks) if system_chunks else None
    return system, out


def _openai_tool_to_anthropic(tool: dict[str, Any]) -> dict[str, Any]:
    if "name" in tool and "input_schema" in tool:
        return tool  # already anthropic-shaped
    function = tool.get("function") or {}
    return {
        "name": function.get("name") or tool.get("name"),
        "description": function.get("description") or tool.get("description") or "",
        "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
    }


def _parse_anthropic_event(event: Any) -> LLMStreamChunk | None:
    event_type = getattr(event, "type", None)
    if event_type == "content_block_delta":
        delta = getattr(event, "delta", None)
        delta_type = getattr(delta, "type", None)
        if delta_type == "text_delta":
            return LLMStreamChunk(content_delta=getattr(delta, "text", "") or None, raw=event)
        if delta_type == "thinking_delta":
            return LLMStreamChunk(
                reasoning_delta=getattr(delta, "thinking", "") or None,
                raw=event,
            )
        if delta_type == "input_json_delta":
            partial = getattr(delta, "partial_json", "") or ""
            try:
                args = json.loads(partial) if partial else {}
            except json.JSONDecodeError:
                args = {}
            return LLMStreamChunk(
                tool_call_delta=ToolCall(id="", name="", arguments=args),
                raw=event,
            )
    if event_type == "content_block_start":
        block = getattr(event, "content_block", None)
        if getattr(block, "type", None) == "tool_use":
            return LLMStreamChunk(
                tool_call_delta=ToolCall(
                    id=getattr(block, "id", "") or "",
                    name=getattr(block, "name", "") or "",
                    arguments={},
                ),
                raw=event,
            )
    if event_type == "message_delta":
        delta = getattr(event, "delta", None)
        finish_reason = getattr(delta, "stop_reason", None)
        if finish_reason:
            return LLMStreamChunk(finish_reason=str(finish_reason), raw=event)
    return None
