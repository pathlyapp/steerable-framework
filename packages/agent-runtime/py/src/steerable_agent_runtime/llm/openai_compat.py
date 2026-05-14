"""OpenAI-compatible chat-completions provider.

Covers the OpenAI API itself plus any vendor that exposes a `/chat/completions`
endpoint matching the OpenAI v1 schema:

  * Ollama (`http://localhost:11434/v1`)
  * vLLM
  * SiliconFlow
  * DeepSeek
  * 万界 wanjiedata (OpenAI-compatible path)

The implementation is dependency-light: it uses `httpx` directly so end users do
not need to install the heavyweight `openai` SDK.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from steerable_agent_protocol.generated import ToolCall

from . import LLMMessage, LLMStreamChunk, LLMUsage


@dataclass(slots=True)
class OpenAICompatProvider:
    """OpenAI-compatible chat-completions provider."""

    name: str
    model: str
    base_url: str
    api_key: str | None = None
    default_temperature: float | None = None

    def __post_init__(self) -> None:
        if not self.base_url:
            raise ValueError("OpenAICompatProvider requires base_url")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> tuple[LLMMessage, LLMUsage]:
        import httpx  # local import — keeps the runtime importable without httpx

        body = self._build_body(
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
            extra=kwargs,
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers=self._headers(),
                json=body,
            )
            response.raise_for_status()
            payload = response.json()

        choice = payload["choices"][0]
        message = choice["message"]
        usage = payload.get("usage") or {}
        out = LLMMessage(
            role="assistant",
            content=message.get("content") or "",
            tool_calls=_decode_tool_calls(message.get("tool_calls")),
        )
        return out, LLMUsage(
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            total_tokens=int(usage.get("total_tokens", 0) or 0),
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
        import httpx

        body = self._build_body(
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            extra=kwargs,
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(None)) as client:
            async with client.stream(
                "POST",
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers=self._headers(),
                json=body,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    if line.startswith(":"):  # comment/keepalive
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if line == "[DONE]":
                        return
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    parsed = _parse_stream_chunk(chunk)
                    if parsed is not None:
                        yield parsed

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _build_body(
        self,
        *,
        messages: Sequence[LLMMessage],
        tools: Iterable[dict[str, Any]] | None,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [_encode_message(m) for m in messages],
            "stream": stream,
        }
        eff_temperature = temperature if temperature is not None else self.default_temperature
        if eff_temperature is not None:
            body["temperature"] = eff_temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if tools is not None:
            tools_list = list(tools)
            if tools_list:
                body["tools"] = tools_list
        body.update(extra)
        return body


# ---------------------------------------------------------------------------
# Wire-format helpers (kept pure functions for unit-testability)
# ---------------------------------------------------------------------------


def _encode_message(message: LLMMessage) -> dict[str, Any]:
    out: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.name is not None:
        out["name"] = message.name
    if message.tool_call_id is not None:
        out["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments),
                },
            }
            for tc in message.tool_calls
        ]
    return out


def _decode_tool_calls(value: Any) -> list[ToolCall] | None:
    if not value:
        return None
    out: list[ToolCall] = []
    for item in value:
        function = item.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except (TypeError, json.JSONDecodeError):
            arguments = {}
        out.append(
            ToolCall(
                id=item.get("id") or "",
                name=function.get("name") or "",
                arguments=arguments,
            )
        )
    return out or None


def _parse_stream_chunk(chunk: dict[str, Any]) -> LLMStreamChunk | None:
    choices = chunk.get("choices") or []
    if not choices:
        usage = chunk.get("usage")
        if usage:
            return LLMStreamChunk(
                usage=LLMUsage(
                    prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                    completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                    total_tokens=int(usage.get("total_tokens", 0) or 0),
                ),
                raw=chunk,
            )
        return None

    choice = choices[0]
    delta = choice.get("delta") or {}
    finish_reason = choice.get("finish_reason")
    content = delta.get("content")
    reasoning = delta.get("reasoning_content")
    tool_call_delta: ToolCall | None = None
    raw_tool_calls = delta.get("tool_calls")
    if raw_tool_calls:
        first = raw_tool_calls[0]
        function = first.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except (TypeError, json.JSONDecodeError):
            arguments = {}
        tool_call_delta = ToolCall(
            id=first.get("id") or "",
            name=function.get("name") or "",
            arguments=arguments,
        )
    return LLMStreamChunk(
        content_delta=content,
        reasoning_delta=reasoning,
        tool_call_delta=tool_call_delta,
        finish_reason=finish_reason,
        raw=chunk,
    )
