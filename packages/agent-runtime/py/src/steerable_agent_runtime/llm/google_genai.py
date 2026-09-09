"""Google Gemini native provider (Generative Language API).

Covers ``POST /v1beta/models/{model}:generateContent`` and its SSE sibling
``:streamGenerateContent?alt=sse`` — the native Gemini wire, where thinking
config, safety settings, and cached content are first-class (the
OpenAI-compatible shim Google also serves exposes only a subset).

The wire is *part-based*: every content is a list of parts (text /
functionCall / functionResponse / inlineData), roles are ``user``/``model``,
and streamed function calls arrive whole in one chunk rather than as
argument fragments.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from steerable_agent_protocol.generated import ToolCall

from . import LLMMessage, LLMStreamChunk, LLMUsage
from .errors import LLMError, classify_http_status, parse_retry_after_ms
from .parts import ImagePart, TextPart
from .presets import ProviderPreset, preset_for
from .system_proxy import client_env_kwargs

logger = logging.getLogger(__name__)

_DEFAULT_STREAM_READ_SEC = 300.0
_DEFAULT_CONNECT_SEC = 30.0

_FINISH_REASON_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
}


def _timeout_sec(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _stream_timeout():
    import httpx

    return httpx.Timeout(
        connect=_timeout_sec("STEERABLE_LLM_CONNECT_TIMEOUT_SEC", _DEFAULT_CONNECT_SEC),
        read=_timeout_sec(
            "STEERABLE_LLM_STREAM_READ_TIMEOUT_SEC", _DEFAULT_STREAM_READ_SEC
        ),
        write=30.0,
        pool=30.0,
    )


@dataclass(slots=True)
class GoogleGenAIProvider:
    """Google Gemini native provider.

    ``base_url`` defaults to the public Generative Language endpoint; Vertex
    AI Express mode works by pointing it at the Vertex host. ``preset``
    behaves exactly like ``OpenAICompatProvider``'s.
    """

    name: str
    model: str
    base_url: str = "https://generativelanguage.googleapis.com"
    api_key: str | None = None
    default_temperature: float | None = None
    preset: ProviderPreset | Literal["auto", "off"] = "auto"

    def __post_init__(self) -> None:
        if not self.base_url:
            raise ValueError("GoogleGenAIProvider requires base_url")

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
            extra=kwargs,
        )
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(60.0),
                **client_env_kwargs(self.base_url),
            ) as client:
                response = await client.post(
                    self._url(stream=False),
                    headers=self._headers(),
                    json=body,
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise self._http_error(exc, body_text=response.text) from exc
                payload = response.json()
        except httpx.TransportError as exc:
            raise LLMError(
                f"{self.name}: transport error: {exc}",
                kind="transport",
                provider=self.name,
            ) from exc

        return _decode_response(payload)

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
            extra=kwargs,
        )
        try:
            async with (
                httpx.AsyncClient(
                    timeout=_stream_timeout(),
                    **client_env_kwargs(self.base_url),
                ) as client,
                client.stream(
                    "POST",
                    self._url(stream=True),
                    headers=self._headers(),
                    json=body,
                ) as response,
            ):
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    await response.aread()
                    raise self._http_error(exc, body_text=response.text) from exc
                async for line in response.aiter_lines():
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if line == "[DONE]":
                        break
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    parsed = _parse_stream_chunk(chunk)
                    if parsed is not None:
                        yield parsed
        except httpx.TransportError as exc:
            raise LLMError(
                f"{self.name}: transport error: {exc}",
                kind="transport",
                provider=self.name,
            ) from exc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _url(self, *, stream: bool) -> str:
        method = "streamGenerateContent?alt=sse" if stream else "generateContent"
        return f"{self.base_url.rstrip('/')}/v1beta/models/{self.model}:{method}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["x-goog-api-key"] = self.api_key
        return headers

    def _http_error(self, exc: Any, *, body_text: str) -> LLMError:
        """Classify an httpx status failure into the error taxonomy."""
        status = exc.response.status_code
        kind = classify_http_status(status, body_text)
        snippet = (body_text or "").strip().replace("\n", " ")[:300]
        headers = getattr(exc.response, "headers", None) or {}
        retry_after = parse_retry_after_ms(headers.get("retry-after"))
        return LLMError(
            f"{self.name}: HTTP {status} ({kind})"
            + (f": {snippet}" if snippet else ""),
            kind=kind,
            status_code=status,
            provider=self.name,
            retry_after_ms=retry_after,
        )

    def _build_body(
        self,
        *,
        messages: Sequence[LLMMessage],
        tools: Iterable[dict[str, Any]] | None,
        temperature: float | None,
        max_tokens: int | None,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        if self.preset == "off":
            preset = None
        elif self.preset == "auto":
            preset = preset_for(self.base_url, self.model)
        else:
            preset = self.preset
        system_text, contents = _encode_contents(messages)
        body: dict[str, Any] = {"contents": contents}
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text}]}
        generation: dict[str, Any] = {}
        eff_temperature = temperature if temperature is not None else self.default_temperature
        if eff_temperature is None and preset is not None:
            eff_temperature = preset.temperature
        if eff_temperature is not None:
            generation["temperature"] = eff_temperature
        eff_max_tokens = max_tokens
        if eff_max_tokens is None and preset is not None:
            eff_max_tokens = preset.max_tokens
        if eff_max_tokens is not None:
            generation["maxOutputTokens"] = eff_max_tokens
        if preset is not None and preset.top_p is not None:
            generation["topP"] = preset.top_p
        # Thinking config is model-family specific (budget tokens vs levels);
        # callers pass it through ``generationConfig`` in extra_body rather
        # than the runtime guessing a mapping.
        if generation:
            body["generationConfig"] = generation
        if tools is not None:
            declarations = [_function_declaration(t) for t in tools]
            declarations = [d for d in declarations if d is not None]
            if declarations:
                body["tools"] = [{"functionDeclarations": declarations}]
        body.update(extra)
        if preset is not None:
            for key, value in preset.extra_body.items():
                if key not in body:
                    body[key] = value
        return body


# ---------------------------------------------------------------------------
# Wire-format helpers (kept pure functions for unit-testability)
# ---------------------------------------------------------------------------


def _function_declaration(tool: dict[str, Any]) -> dict[str, Any] | None:
    """Convert an OpenAI function-calling tool to a Gemini declaration."""
    function = tool.get("function")
    if not isinstance(function, dict):
        return None
    out: dict[str, Any] = {"name": function.get("name") or ""}
    if function.get("description"):
        out["description"] = function["description"]
    parameters = function.get("parameters")
    if isinstance(parameters, dict) and parameters:
        out["parameters"] = parameters
    return out


def _encode_parts(message: LLMMessage) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for part in message.content:
        if isinstance(part, TextPart):
            parts.append({"text": part.text})
        elif isinstance(part, ImagePart):
            if part.is_url:
                parts.append({"fileData": {"fileUri": part.source, "mimeType": part.media_type}})
            else:
                parts.append(
                    {"inlineData": {"mimeType": part.media_type, "data": part.source}}
                )
    return parts


def _encode_contents(messages: Sequence[LLMMessage]) -> tuple[str, list[dict[str, Any]]]:
    """Split system text out; map roles to user/model contents.

    Gemini has no tool-call ids: a functionResponse matches its call by
    *name*, so tool results must carry the tool name (``LLMMessage.name``).
    """
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            text = message.content_text
            if text:
                system_parts.append(text)
            continue
        if message.role == "tool":
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": message.name or message.tool_call_id or "",
                                "response": {"result": message.content_text},
                            }
                        }
                    ],
                }
            )
            continue
        if message.role == "assistant":
            parts = _encode_parts(message)
            for call in message.tool_calls or []:
                parts.append(
                    {"functionCall": {"name": call.name, "args": call.arguments}}
                )
            if parts:
                contents.append({"role": "model", "parts": parts})
            continue
        contents.append({"role": "user", "parts": _encode_parts(message)})
    return "\n\n".join(system_parts), contents


def _parse_usage(usage: dict[str, Any]) -> LLMUsage:
    prompt = int(usage.get("promptTokenCount", 0) or 0)
    completion = int(usage.get("candidatesTokenCount", 0) or 0)
    return LLMUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=int(usage.get("totalTokenCount", 0) or 0) or prompt + completion,
        cached_prompt_tokens=int(usage.get("cachedContentTokenCount", 0) or 0),
    )


def _parse_candidate_parts(parts: list[Any]) -> tuple[str | None, str | None, list[ToolCall]]:
    """Split candidate parts into (text, thought, tool calls)."""
    text_out: list[str] = []
    thought_out: list[str] = []
    calls: list[ToolCall] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        function_call = part.get("functionCall")
        if isinstance(function_call, dict):
            args = function_call.get("args")
            calls.append(
                ToolCall(
                    # Gemini assigns no call id; the name is the correlation
                    # key on the functionResponse round-trip.
                    id=str(function_call.get("name") or ""),
                    name=str(function_call.get("name") or ""),
                    arguments=args if isinstance(args, dict) else {},
                )
            )
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            if part.get("thought") is True:
                thought_out.append(text)
            else:
                text_out.append(text)
    return (
        "".join(text_out) or None,
        "".join(thought_out) or None,
        calls,
    )


def _parse_stream_chunk(chunk: dict[str, Any]) -> LLMStreamChunk | None:
    candidates = chunk.get("candidates") or []
    usage = chunk.get("usageMetadata")
    content_delta: str | None = None
    reasoning_delta: str | None = None
    finish_reason: str | None = None
    tool_call_delta: ToolCall | None = None
    if candidates:
        candidate = candidates[0]
        content = candidate.get("content") or {}
        text, thought, calls = _parse_candidate_parts(content.get("parts") or [])
        content_delta = text
        reasoning_delta = thought
        if calls:
            tool_call_delta = calls[0]
        raw_finish = candidate.get("finishReason")
        if raw_finish:
            finish_reason = _FINISH_REASON_MAP.get(str(raw_finish), "stop")
    if (
        content_delta is None
        and reasoning_delta is None
        and tool_call_delta is None
        and finish_reason is None
        and usage is None
    ):
        return None
    return LLMStreamChunk(
        content_delta=content_delta,
        reasoning_delta=reasoning_delta,
        tool_call_delta=tool_call_delta,
        finish_reason=finish_reason,
        usage=_parse_usage(usage) if usage else None,
        raw=chunk,
    )


def _decode_response(payload: dict[str, Any]) -> tuple[LLMMessage, LLMUsage]:
    """Decode a non-streaming generateContent payload."""
    candidates = payload.get("candidates") or []
    text = ""
    calls: list[ToolCall] = []
    if candidates:
        content = (candidates[0].get("content") or {})
        text_part, _thought, calls = _parse_candidate_parts(content.get("parts") or [])
        text = text_part or ""
    out = LLMMessage.text_of(
        "assistant",
        text,
        tool_calls=calls or None,
    )
    return out, _parse_usage(payload.get("usageMetadata") or {})
