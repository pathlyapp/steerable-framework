"""OpenAI Responses API provider.

Covers the Responses wire protocol (``POST /v1/responses``) — OpenAI's
stateful-successor to chat/completions and the only API where the o-series /
gpt-5 reasoning models expose their full capability set. Also serves xAI's
Responses endpoint.

Unlike chat/completions, Responses is *item-based*: the input is a list of
typed items (message / function_call / function_call_output / reasoning), and
the stream is a sequence of typed events rather than choice deltas. The
provider is dependency-light (raw httpx + SSE), mirroring
``OpenAICompatProvider``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from steerable_agent_protocol.generated import ToolCall

from ..model_info import ReasoningEffortUnsupported, clamp_reasoning_effort
from . import LLMMessage, LLMStreamChunk, LLMUsage
from .errors import LLMError, classify_http_status, parse_retry_after_ms
from .parts import ImagePart, TextPart
from .presets import ProviderPreset, preset_for
from .system_proxy import client_env_kwargs

logger = logging.getLogger(__name__)

_DEFAULT_STREAM_READ_SEC = 300.0
_DEFAULT_CONNECT_SEC = 30.0


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
class OpenAIResponsesProvider:
    """OpenAI Responses API provider.

    ``preset`` selects the vendor-preset layer exactly like
    ``OpenAICompatProvider``: ``"auto"`` matches ``llm.presets.preset_for``,
    ``"off"`` disables it, a ``ProviderPreset`` instance pins one.
    """

    name: str
    model: str
    base_url: str
    api_key: str | None = None
    default_temperature: float | None = None
    preset: ProviderPreset | Literal["auto", "off"] = "auto"
    #: Per-request reasoning effort from the host's model picker; wins over
    #: ``STEERABLE_REASONING_EFFORT`` and the preset default. Explicitly
    #: requested effort is validated strict (``clamp_reasoning_effort``) —
    #: a level the model cannot honor fails the request instead of being
    #: silently dropped (EVALS 2.5.22).
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if not self.base_url:
            raise ValueError("OpenAIResponsesProvider requires base_url")

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
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(60.0),
                **client_env_kwargs(self.base_url),
            ) as client:
                response = await client.post(
                    f"{self.base_url.rstrip('/')}/responses",
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

        if payload.get("status") == "failed" or payload.get("error"):
            error = payload.get("error") or {}
            raise LLMError(
                f"{self.name}: response failed: {error.get('message') or 'unknown'}",
                kind="server",
                provider=self.name,
            )
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
            stream=True,
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
                    f"{self.base_url.rstrip('/')}/responses",
                    headers=self._headers(),
                    json=body,
                ) as response,
            ):
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    await response.aread()
                    raise self._http_error(exc, body_text=response.text) from exc
                assembler = _ResponsesToolCallAssembler()
                async for line in response.aiter_lines():
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if line == "[DONE]":
                        break
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    event_type = event.get("type") or ""
                    if event_type in ("response.failed", "error"):
                        error = (event.get("response") or {}).get("error") or event.get("error") or {}
                        raise LLMError(
                            f"{self.name}: stream failed: {error.get('message') or event_type}",
                            kind="server",
                            provider=self.name,
                        )
                    assembler.observe(event)
                    parsed = _parse_response_event(event)
                    if parsed is not None:
                        yield parsed
                for call in assembler.flush():
                    yield LLMStreamChunk(tool_call_delta=call)
        except httpx.TransportError as exc:
            raise LLMError(
                f"{self.name}: transport error: {exc}",
                kind="transport",
                provider=self.name,
            ) from exc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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
        if self.preset == "off":
            preset = None
        elif self.preset == "auto":
            preset = preset_for(self.base_url, self.model)
        else:
            preset = self.preset
        instructions, items = _encode_input(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "input": items,
            "stream": stream,
            # The runtime resends the full transcript every turn; persisting
            # responses server-side would only buy data retention.
            "store": False,
        }
        if instructions:
            body["instructions"] = instructions
        if stream:
            # Usage arrives on ``response.completed`` regardless; no
            # stream_options equivalent exists on this wire.
            pass
        eff_temperature = temperature if temperature is not None else self.default_temperature
        if eff_temperature is None and preset is not None:
            eff_temperature = preset.temperature
        if eff_temperature is not None:
            body["temperature"] = eff_temperature
        eff_max_tokens = max_tokens
        if eff_max_tokens is None and preset is not None:
            eff_max_tokens = preset.max_tokens
        if eff_max_tokens is not None:
            body["max_output_tokens"] = eff_max_tokens
        if tools is not None:
            tools_list = [_responses_tool(t) for t in tools]
            if tools_list:
                body["tools"] = tools_list
        body.update(extra)
        if preset is not None:
            if preset.top_p is not None and "top_p" not in body:
                body["top_p"] = preset.top_p
            for key, value in preset.extra_body.items():
                if key not in body:
                    body[key] = value
        # Reasoning lives under ``reasoning.effort`` on this wire.
        # Precedence: the host's per-request pick, then the env var, then
        # the preset's documented default. An explicit request is validated
        # strict against the resolved catalog entry — a level the model
        # cannot honor raises instead of being silently dropped
        # (EVALS 2.5.22), same as the chat-completions path.
        requested_effort = (
            self.reasoning_effort
            or os.environ.get("STEERABLE_REASONING_EFFORT", "")
            or (preset.reasoning_effort if preset is not None else "")
        )
        effort: str | None = None
        if requested_effort:
            try:
                effort = clamp_reasoning_effort(
                    self.model,
                    requested_effort,
                    provider=self.name,
                    base_url=self.base_url,
                    strict=True,
                )
            except ReasoningEffortUnsupported as exc:
                raise LLMError(
                    f"{self.name}: {exc}",
                    kind="invalid_request",
                    provider=self.name,
                ) from exc
        if effort and "reasoning" not in body:
            body["reasoning"] = {"effort": effort}
        if "include" not in body:
            # With store=False the reasoning items must come back in-band or
            # the next tool round-trip loses the model's chain of thought.
            body["include"] = ["reasoning.encrypted_content"]
        return body


# ---------------------------------------------------------------------------
# Wire-format helpers (kept pure functions for unit-testability)
# ---------------------------------------------------------------------------


def _responses_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Flatten an OpenAI function-calling tool into the Responses shape."""
    function = tool.get("function")
    if isinstance(function, dict):
        out: dict[str, Any] = {"type": "function", "name": function.get("name") or ""}
        if function.get("description"):
            out["description"] = function["description"]
        if function.get("parameters") is not None:
            out["parameters"] = function["parameters"]
        return out
    return tool


def _encode_input(messages: Sequence[LLMMessage]) -> tuple[str, list[dict[str, Any]]]:
    """Split system text into ``instructions``; the rest become input items."""
    system_parts: list[str] = []
    items: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            text = message.content_text
            if text:
                system_parts.append(text)
            continue
        items.extend(_encode_message_items(message))
    return "\n\n".join(system_parts), items


def _encode_content_parts(message: LLMMessage, *, output: bool) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for part in message.content:
        if isinstance(part, TextPart):
            kind = "output_text" if output else "input_text"
            parts.append({"type": kind, "text": part.text})
        elif isinstance(part, ImagePart):
            url = (
                part.source
                if part.is_url
                else f"data:{part.media_type};base64,{part.source}"
            )
            parts.append({"type": "input_image", "image_url": url})
    return parts


def _encode_message_items(message: LLMMessage) -> list[dict[str, Any]]:
    if message.role == "tool":
        return [
            {
                "type": "function_call_output",
                "call_id": message.tool_call_id or "",
                "output": message.content_text,
            }
        ]
    if message.role == "assistant":
        items: list[dict[str, Any]] = []
        # Reasoning items round-trip unmodified (encrypted content included)
        # so a reasoning model can continue across tool turns.
        if message.reasoning_details:
            for detail in message.reasoning_details:
                if isinstance(detail, dict) and detail.get("type") == "reasoning":
                    items.append(detail)
        content = _encode_content_parts(message, output=True)
        if content:
            items.append({"type": "message", "role": "assistant", "content": content})
        for call in message.tool_calls or []:
            items.append(
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": json.dumps(call.arguments),
                }
            )
        return items
    # user (and any future non-system role) → input message
    content = _encode_content_parts(message, output=False)
    return [{"type": "message", "role": message.role, "content": content}]


class _ResponsesToolCallAssembler:
    """Assemble streamed function_call items.

    ``response.output_item.added`` carries call_id + name; the arguments JSON
    string arrives split across ``response.function_call_arguments.delta``
    events keyed by ``output_index`` — parsing each fragment would drop the
    command, same failure mode as chat/completions.
    """

    def __init__(self) -> None:
        self._buf: dict[int, dict[str, str]] = {}

    def observe(self, event: dict[str, Any]) -> None:
        event_type = event.get("type") or ""
        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") != "function_call":
                return
            idx = int(event.get("output_index") or 0)
            slot = self._buf.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            slot["id"] = str(item.get("call_id") or item.get("id") or "")
            slot["name"] = str(item.get("name") or "")
            arguments = item.get("arguments")
            if isinstance(arguments, str) and arguments:
                slot["arguments"] = arguments
        elif event_type == "response.function_call_arguments.delta":
            idx = int(event.get("output_index") or 0)
            slot = self._buf.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            delta = event.get("delta")
            if isinstance(delta, str):
                slot["arguments"] += delta
        elif event_type == "response.function_call_arguments.done":
            idx = int(event.get("output_index") or 0)
            slot = self._buf.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            arguments = event.get("arguments")
            if isinstance(arguments, str) and arguments:
                slot["arguments"] = arguments

    def flush(self) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for idx in sorted(self._buf):
            slot = self._buf[idx]
            if not slot["name"]:
                continue
            try:
                arguments = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            calls.append(ToolCall(id=slot["id"], name=slot["name"], arguments=arguments))
        self._buf.clear()
        return calls


def _parse_usage(usage: dict[str, Any]) -> LLMUsage:
    details = usage.get("input_tokens_details") or {}
    return LLMUsage(
        prompt_tokens=int(usage.get("input_tokens", 0) or 0),
        completion_tokens=int(usage.get("output_tokens", 0) or 0),
        total_tokens=int(usage.get("total_tokens", 0) or 0),
        cached_prompt_tokens=int(details.get("cached_tokens", 0) or 0),
    )


def _parse_response_event(event: dict[str, Any]) -> LLMStreamChunk | None:
    """Map one Responses SSE event to a stream chunk (or None to skip)."""
    event_type = event.get("type") or ""
    if event_type == "response.output_text.delta":
        delta = event.get("delta")
        return LLMStreamChunk(content_delta=delta or None, raw=event)
    if event_type in (
        "response.reasoning_text.delta",
        "response.reasoning_summary_text.delta",
    ):
        delta = event.get("delta")
        return LLMStreamChunk(reasoning_delta=delta or None, raw=event)
    if event_type == "response.output_item.done":
        item = event.get("item") or {}
        if item.get("type") == "reasoning":
            return LLMStreamChunk(reasoning_details=[item], raw=event)
        return None
    if event_type == "response.completed":
        response = event.get("response") or {}
        usage = response.get("usage")
        finish = "stop"
        if response.get("status") == "incomplete":
            details = response.get("incomplete_details") or {}
            finish = "length" if details.get("reason") == "max_output_tokens" else "stop"
        return LLMStreamChunk(
            finish_reason=finish,
            usage=_parse_usage(usage) if usage else None,
            raw=event,
        )
    return None


def _decode_response(payload: dict[str, Any]) -> tuple[LLMMessage, LLMUsage]:
    """Decode a non-streaming Responses payload into message + usage."""
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    reasoning_items: list[Any] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    text_parts.append(str(part.get("text") or ""))
        elif item_type == "function_call":
            try:
                arguments = json.loads(item.get("arguments") or "{}")
            except (TypeError, json.JSONDecodeError):
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            tool_calls.append(
                ToolCall(
                    id=str(item.get("call_id") or item.get("id") or ""),
                    name=str(item.get("name") or ""),
                    arguments=arguments,
                )
            )
        elif item_type == "reasoning":
            reasoning_items.append(item)
    out = LLMMessage.text_of(
        "assistant",
        "".join(text_parts),
        tool_calls=tool_calls or None,
        reasoning_details=reasoning_items or None,
    )
    return out, _parse_usage(payload.get("usage") or {})
