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
import logging
import os
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from steerable_agent_protocol.generated import ToolCall

from ..model_info import clamp_reasoning_effort
from . import LLMMessage, LLMStreamChunk, LLMUsage
from .compat import OpenAICompatFlags
from .errors import LLMError, classify_http_status, parse_retry_after_ms
from .parts import ImagePart, TextPart

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


def _glm_z_ai_host(base_url: str | None) -> bool:
    host = (base_url or "").lower()
    return "z.ai" in host or "bigmodel.cn" in host


def _openrouter_host(base_url: str | None) -> bool:
    return "openrouter.ai" in (base_url or "").lower()


def _z_ai_tool_choice_auto_only(model: str, base_url: str | None) -> bool:
    """Z.AI rejects ``tool_choice=required`` with HTTP 400 (must be auto)."""
    if _glm_z_ai_host(base_url):
        return True
    lowered = (model or "").lower()
    return _openrouter_host(base_url) and ("z-ai" in lowered or "glm" in lowered)


def _env_flag(name: str) -> bool | None:
    raw = os.environ.get(name, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return None


def _openrouter_provider_prefs() -> dict[str, Any] | None:
    """Pin OpenRouter to named upstreams (Harbor TB: Z.ai, not Relace)."""
    raw = os.environ.get("STEERABLE_OPENROUTER_PROVIDER", "").strip()
    if not raw:
        return None
    order = [part.strip() for part in raw.split(",") if part.strip()]
    if not order:
        return None
    prefs: dict[str, Any] = {"order": order, "only": order}
    fallbacks = _env_flag("STEERABLE_OPENROUTER_ALLOW_FALLBACKS")
    if fallbacks is not None:
        prefs["allow_fallbacks"] = fallbacks
    require = _env_flag("STEERABLE_OPENROUTER_REQUIRE_PARAMETERS")
    if require is not None:
        prefs["require_parameters"] = require
    return prefs


def _stream_timeout():
    """Idle gap between SSE lines; ``Timeout(None)`` left hung thinks uncapped."""
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
class OpenAICompatProvider:
    """OpenAI-compatible chat-completions provider.

    ``compat`` carries the vendor's divergences from the reference API as
    data (see ``llm.compat``); ``None`` selects the reference defaults.
    """

    name: str
    model: str
    base_url: str
    api_key: str | None = None
    default_temperature: float | None = None
    compat: OpenAICompatFlags | None = None

    def __post_init__(self) -> None:
        if not self.base_url:
            raise ValueError("OpenAICompatProvider requires base_url")
        if self.compat is None:
            self.compat = OpenAICompatFlags()

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
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
                response = await client.post(
                    f"{self.base_url.rstrip('/')}/chat/completions",
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

        choice = payload["choices"][0]
        message = choice["message"]
        out = LLMMessage.text_of(
            "assistant",
            message.get("content") or "",
            tool_calls=_decode_tool_calls(message.get("tool_calls")),
            reasoning=_reasoning_text(message, ("reasoning_content", "reasoning")),
            reasoning_details=_reasoning_details_list(message.get("reasoning_details")),
        )
        return out, _parse_usage(payload.get("usage") or {}, compat=self.compat)

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
                httpx.AsyncClient(timeout=_stream_timeout()) as client,
                client.stream(
                    "POST",
                    f"{self.base_url.rstrip('/')}/chat/completions",
                    headers=self._headers(),
                    json=body,
                ) as response,
            ):
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    await response.aread()
                    raise self._http_error(exc, body_text=response.text) from exc
                assembler = _OpenAIToolCallAssembler()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    if line.startswith(":"):  # comment/keepalive
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if line == "[DONE]":
                        for call in assembler.flush():
                            yield LLMStreamChunk(tool_call_delta=call)
                        return
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    assembler.observe(chunk)
                    parsed = _parse_stream_chunk(chunk, compat=self.compat)
                    if parsed is None:
                        continue
                    # Fragments are assembled below; do not dispatch per chunk.
                    if parsed.tool_call_delta is not None:
                        parsed = LLMStreamChunk(
                            content_delta=parsed.content_delta,
                            reasoning_delta=parsed.reasoning_delta,
                            reasoning_details=parsed.reasoning_details,
                            finish_reason=parsed.finish_reason,
                            usage=parsed.usage,
                            raw=parsed.raw,
                        )
                    if (
                        parsed.content_delta
                        or parsed.reasoning_delta
                        or parsed.reasoning_details
                        or parsed.finish_reason
                        or parsed.usage is not None
                    ):
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
        referer = os.environ.get("STEERABLE_HTTP_REFERER", "").strip()
        if referer:
            headers["HTTP-Referer"] = referer
        title = os.environ.get("STEERABLE_HTTP_TITLE", "").strip()
        if title:
            headers["X-Title"] = title
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
        compat = self.compat or OpenAICompatFlags()
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [_encode_message(m) for m in messages],
            "stream": stream,
        }
        if stream and compat.supports_usage_in_streaming:
            # OpenAI-streaming sends no usage by default; without the final
            # usage chunk both budget accounting (loop consumes chunk.usage)
            # and usage calibration are blind. Supported by OpenAI, Ollama,
            # vLLM, DeepSeek; strict vendors flag out via compat.
            body["stream_options"] = {"include_usage": True}
        eff_temperature = temperature if temperature is not None else self.default_temperature
        if eff_temperature is not None and compat.supports_temperature:
            body["temperature"] = eff_temperature
        if max_tokens is not None:
            body[compat.max_tokens_field] = max_tokens
        if tools is not None:
            tools_list = list(tools)
            if tools_list:
                body["tools"] = tools_list
        body.update(extra)
        # Z.AI (direct or OpenRouter pin) 400s ``tool_choice=required``.
        # Harbor still logs the hook; the wire must send auto or the trial
        # dies on round 0 (failed-prev 33335200327).
        if body.get("tool_choice") == "required" and _z_ai_tool_choice_auto_only(
            self.model, self.base_url
        ):
            body["tool_choice"] = "auto"
        # W6-8: clamp the env-requested reasoning effort to a level the model
        # actually supports (structured ModelInfo replaces the raw env
        # passthrough). A model with no reasoning knob gets no parameter at
        # all — sending one would be an unsupported-field error on strict APIs.
        effort = clamp_reasoning_effort(
            self.model, os.environ.get("STEERABLE_REASONING_EFFORT", "")
        )
        if effort and compat.supports_reasoning_effort:
            # GLM-5.3 default is ``max``; ``high`` is a downgrade. TB uses max.
            if "reasoning_effort" not in body:
                body["reasoning_effort"] = effort
            # OpenRouter documents ``reasoning.effort``; some providers ignore
            # the Z.AI ``reasoning_effort`` pass-through.
            if _openrouter_host(self.base_url) and "reasoning" not in body:
                # ``exclude: false`` keeps thinking tokens in the response so
                # we can round-trip ``reasoning_details`` after tool turns.
                body["reasoning"] = {"effort": effort, "exclude": False}
        if _openrouter_host(self.base_url) and "provider" not in body:
            prefs = _openrouter_provider_prefs()
            if prefs:
                body["provider"] = prefs
        if _glm_z_ai_host(self.base_url) and "thinking" not in body:
            # Forced-on for GLM-5.3; omitting is fine, disabling 400s.
            body["thinking"] = {"type": "enabled"}
            if stream:
                body["tool_stream"] = True
        return body


# ---------------------------------------------------------------------------
# Wire-format helpers (kept pure functions for unit-testability)
# ---------------------------------------------------------------------------


class _OpenAIToolCallAssembler:
    """Concatenate streamed ``function.arguments`` strings, then json.loads.

    OpenAI-compatible SSE sends one tool-call object per index; ``arguments``
    is a JSON *string* split across chunks. Parsing each fragment drops the
    command.
    """

    def __init__(self) -> None:
        self._buf: dict[int, dict[str, str]] = {}

    def observe(self, chunk: dict[str, Any]) -> None:
        choices = chunk.get("choices") or []
        if not choices:
            return
        delta = (choices[0].get("delta") or {}) if isinstance(choices[0], dict) else {}
        for item in delta.get("tool_calls") or []:
            if not isinstance(item, dict):
                continue
            idx = int(item.get("index") or 0)
            slot = self._buf.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if item.get("id"):
                slot["id"] = str(item["id"])
            function = item.get("function") or {}
            if not isinstance(function, dict):
                continue
            if function.get("name"):
                slot["name"] += str(function["name"])
            raw_args = function.get("arguments")
            if isinstance(raw_args, str) and raw_args:
                slot["arguments"] += raw_args
            elif isinstance(raw_args, dict):
                slot["arguments"] = json.dumps(raw_args)

    def flush(self) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for idx in sorted(self._buf):
            slot = self._buf[idx]
            name = _sanitize_tool_name(slot["name"])
            if not name:
                continue
            raw = slot["arguments"] or "{}"
            try:
                arguments = json.loads(raw)
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            calls.append(ToolCall(id=slot["id"], name=name, arguments=arguments))
        self._buf.clear()
        return calls




# Harmony-format special tokens (gpt-oss family) leak into the tool name
# field when a vendor's chat-completions shim only half-parses the format —
# observed in production as names like "json<|channel|>commentary" or
# "to=functions.exec_command<|channel|>commentary". A valid tool name never
# contains "<|", so everything from the first marker on is leak (including
# the channel word after it, e.g. "commentary"); harmony routing prefixes
# ("to=", "functions.") come off too. This recovers the intended name when
# the leak is partial; when the real name never reached the field (e.g. only
# the <|constrain|> value "json" remains) the residue is garbage that
# ToolRouter rejects with the full valid tool list.
_HARMONY_NAME_PREFIXES = ("to=", "functions.")


def _sanitize_tool_name(name: str) -> str:
    if "<|" not in name and not name.startswith(_HARMONY_NAME_PREFIXES):
        return name
    cleaned = name.split("<|", 1)[0]
    for prefix in _HARMONY_NAME_PREFIXES:
        cleaned = cleaned.removeprefix(prefix)
    cleaned = cleaned.strip()
    if cleaned != name:
        logger.warning("sanitized harmony-leaked tool name %r -> %r", name, cleaned)
    return cleaned


def _encode_content(message: LLMMessage) -> str | list[dict[str, Any]]:
    """Serialize content parts to the OpenAI wire shape.

    Text-only content uses the legacy string shorthand so existing
    conversations keep byte-identical wire bytes; any non-text part switches
    the message to the structured array form.
    """
    parts = message.content
    if all(isinstance(part, TextPart) for part in parts):
        return message.content_text
    out: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, TextPart):
            out.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            url = (
                part.source
                if part.is_url
                else f"data:{part.media_type};base64,{part.source}"
            )
            out.append({"type": "image_url", "image_url": {"url": url}})
    return out


def _encode_message(message: LLMMessage) -> dict[str, Any]:
    out: dict[str, Any] = {"role": message.role, "content": _encode_content(message)}
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
    # OpenRouter: echo reasoning_details unmodified so GLM continues after
    # tools. Prefer the structured block; plaintext is the fallback.
    if message.reasoning_details:
        out["reasoning_details"] = message.reasoning_details
    elif message.reasoning:
        # DeepSeek thinking models require the previous turn's chain of
        # thought to be echoed back under `reasoning_content` (they reject
        # the follow-up request otherwise); other OpenAI-compat endpoints
        # simply ignore the extra key. Keep the generic `reasoning` field
        # too for providers that read it (e.g. OpenRouter fallback path).
        out["reasoning"] = message.reasoning
        out["reasoning_content"] = message.reasoning
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
                name=_sanitize_tool_name(function.get("name") or ""),
                arguments=arguments,
            )
        )
    return out or None


def _resolve_path(obj: dict[str, Any], path: str) -> Any:
    """Resolve a dotted compat path (``a.b.c``) against a decoded JSON object."""
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _parse_usage(
    usage: dict[str, Any], *, compat: OpenAICompatFlags | None = None
) -> LLMUsage:
    """Build LLMUsage from an OpenAI-compatible usage object.

    Cache hits come from OpenAI's ``prompt_tokens_details.cached_tokens``;
    DeepSeek reports them at the top level as ``prompt_cache_hit_tokens``
    instead. The lookup order is compat data (``cached_tokens_fields``), not
    hardcoded here. Providers without cache accounting yield zero.
    """
    flags = compat or OpenAICompatFlags()
    cached = 0
    for path in flags.cached_tokens_fields:
        value = _resolve_path(usage, path)
        if value:
            cached = int(value)
            break
    return LLMUsage(
        prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
        completion_tokens=int(usage.get("completion_tokens", 0) or 0),
        total_tokens=int(usage.get("total_tokens", 0) or 0),
        cached_prompt_tokens=cached,
    )


def _parse_stream_chunk(
    chunk: dict[str, Any], *, compat: OpenAICompatFlags | None = None
) -> LLMStreamChunk | None:
    flags = compat or OpenAICompatFlags()
    choices = chunk.get("choices") or []
    if not choices:
        usage = chunk.get("usage")
        if usage:
            return LLMStreamChunk(
                usage=_parse_usage(usage, compat=flags),
                raw=chunk,
            )
        return None

    choice = choices[0]
    delta = choice.get("delta") or {}
    finish_reason = choice.get("finish_reason")
    content = delta.get("content")
    reasoning = _reasoning_text(delta, flags.reasoning_delta_fields)
    tool_call_delta: ToolCall | None = None
    raw_tool_calls = delta.get("tool_calls")
    if raw_tool_calls:
        first = raw_tool_calls[0]
        function = first.get("function") or {}
        raw_args = function.get("arguments")
        arguments: Any = {}
        if isinstance(raw_args, dict):
            arguments = raw_args
        elif isinstance(raw_args, str) and raw_args:
            try:
                arguments = json.loads(raw_args)
            except json.JSONDecodeError:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        tool_call_delta = ToolCall(
            id=first.get("id") or "",
            name=_sanitize_tool_name(function.get("name") or ""),
            arguments=arguments,
        )
    # OpenRouter (and some other gateways) attach the usage object to a
    # final chunk that still carries a choices array — a duplicate finish
    # marker — instead of sending the OpenAI-style choices:[] usage chunk.
    # Parse it here too or the loop's token/cost accounting stays blind on
    # that wire (live-verified 2026-08-31: z-ai/glm-5.3-flash via OpenRouter
    # delivered usage exactly this way and scored zeros).
    usage = chunk.get("usage")
    return LLMStreamChunk(
        content_delta=content,
        reasoning_delta=reasoning,
        reasoning_details=_reasoning_details_list(delta.get("reasoning_details")),
        tool_call_delta=tool_call_delta,
        finish_reason=finish_reason,
        usage=_parse_usage(usage, compat=flags) if usage else None,
        raw=chunk,
    )


def _reasoning_details_list(value: Any) -> list[Any] | None:
    """Keep a non-empty ``reasoning_details`` array; otherwise omit."""
    if isinstance(value, list) and value:
        return list(value)
    return None


def _reasoning_text(delta: dict[str, Any], fields: tuple[str, ...]) -> str | None:
    """Read the reasoning delta from the first of ``fields`` present.

    Field names are compat data: DeepSeek uses ``reasoning_content``,
    OpenRouter GLM uses ``reasoning`` (see ``OpenAICompatFlags``).
    """
    raw: Any = None
    for field in fields:
        if delta.get(field) is not None:
            raw = delta[field]
            break
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, str) and item:
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
        return "".join(parts) or None
    return None
