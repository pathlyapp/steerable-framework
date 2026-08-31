"""OpenAI-compatible vendor compatibility matrix.

Every place a vendor's "OpenAI-compatible" endpoint diverges from the
reference OpenAI v1 schema is data here, not a branch inside the provider:

* **Request-shape flags** gate what the provider sends. A wrong value is a
  vendor-side HTTP 400, so these are strict booleans/enums.
* **Response-shape fields** name where a value is read from. Defaults are
  tolerant (every known location is tried in order); a vendor entry may
  narrow the tuple to pin documented behavior or extend it for a new shape.

Adding a vendor means adding one entry to ``PROVIDER_COMPAT`` — request
building and stream parsing do not change (that is the acceptance bar).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

_MAX_TOKENS_FIELDS = ("max_tokens", "max_completion_tokens")

#: Single source of truth for the wire surface: ``(wire_key, field_name,
#: kind, description)`` per flag. ``from_dict`` derives its key map from
#: this table and `describe_compat_flags` serves it to hosts, so a new
#: flag appears on the wire and in host settings UIs by adding one row.
#: ``kind`` is ``"bool"``, ``"enum:max_tokens,max_completion_tokens"``-style,
#: or ``"string-list"`` (comma-separated in host UIs).
_FLAG_WIRE_SPEC: tuple[tuple[str, str, str, str], ...] = (
    (
        "supportsUsageInStreaming",
        "supports_usage_in_streaming",
        "bool",
        "Send stream_options.include_usage; disable for vendors that reject it",
    ),
    (
        "maxTokensField",
        "max_tokens_field",
        "enum:max_tokens,max_completion_tokens",
        "Request field that caps the response length",
    ),
    (
        "supportsReasoningEffort",
        "supports_reasoning_effort",
        "bool",
        "Send reasoning_effort when the env clamp yields a level",
    ),
    (
        "supportsTemperature",
        "supports_temperature",
        "bool",
        "Send temperature; disable for fixed-temperature reasoning models",
    ),
    (
        "reasoningDeltaFields",
        "reasoning_delta_fields",
        "string-list",
        "Delta keys read as reasoning text, in preference order",
    ),
    (
        "cachedTokensFields",
        "cached_tokens_fields",
        "string-list",
        "Usage locations read for cached prompt tokens (dotted paths ok)",
    ),
)


def describe_compat_flags() -> list[dict[str, Any]]:
    """Wire-level descriptor of every compat flag for host settings UIs.

    Hosts (desktop settings page) render their compat section from this
    list instead of hardcoding flag names, so the framework stays the
    single source of truth for the flag vocabulary (ALIGN 2.3.3).
    """
    defaults = OpenAICompatFlags()
    return [
        {
            "key": wire,
            "field": field_name,
            "kind": kind,
            "default": getattr(defaults, field_name),
            "description": description,
        }
        for wire, field_name, kind, description in _FLAG_WIRE_SPEC
    ]


@dataclass(frozen=True, slots=True)
class OpenAICompatFlags:
    """Per-vendor compatibility overrides for the OpenAI-compatible path.

    Defaults match the reference OpenAI API; a vendor entry overrides only
    what diverges. Explicit host-passed request fields (extra kwargs) always
    win — flags only gate what the provider itself adds.
    """

    # ── request shape ────────────────────────────────────────────────
    #: Whether ``stream_options: {"include_usage": True}` is accepted.
    #: Without the final usage chunk, budget accounting and calibration are
    #: blind — but strict vendors reject the unknown field outright.
    supports_usage_in_streaming: bool = True
    #: Which request field caps the response. Newer OpenAI models reject
    #: ``max_tokens`` in favor of ``max_completion_tokens``.
    max_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    #: Whether ``reasoning_effort`` may be sent when the env clamp yields a
    #: level. Vendors without a reasoning knob reject the field.
    supports_reasoning_effort: bool = True
    #: Whether ``temperature`` may be sent. Reasoning-only model families
    #: (o-series class) reject it.
    supports_temperature: bool = True

    # ── response shape ───────────────────────────────────────────────
    #: Delta keys read as reasoning text, in preference order. DeepSeek uses
    #: ``reasoning_content``; OpenRouter's GLM path uses ``reasoning``.
    reasoning_delta_fields: tuple[str, ...] = ("reasoning_content", "reasoning")
    #: Usage locations read for cached prompt tokens, in preference order.
    #: Dotted paths resolve nested objects. OpenAI nests under
    #: ``prompt_tokens_details.cached_tokens``; DeepSeek reports top-level
    #: ``prompt_cache_hit_tokens``.
    cached_tokens_fields: tuple[str, ...] = (
        "prompt_tokens_details.cached_tokens",
        "prompt_cache_hit_tokens",
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OpenAICompatFlags:
        """Build flags from a camelCase host payload (sidecar ``compat``
        param). Unknown keys fail loud — a typo'd flag must not silently
        degrade to reference behavior."""
        key_map = {wire: field for wire, field, *_ in _FLAG_WIRE_SPEC}
        unknown = set(data) - set(key_map)
        if unknown:
            raise ValueError(
                f"unknown OpenAICompatFlags keys: {sorted(unknown)} "
                f"(known: {sorted(key_map)})"
            )
        kwargs: dict[str, Any] = {}
        for wire, field in key_map.items():
            if wire not in data:
                continue
            value = data[wire]
            if field in ("reasoning_delta_fields", "cached_tokens_fields"):
                kwargs[field] = tuple(str(v) for v in value)
            elif field == "max_tokens_field":
                if value not in _MAX_TOKENS_FIELDS:
                    raise ValueError(
                        f"maxTokensField must be one of {_MAX_TOKENS_FIELDS}, "
                        f"got {value!r}"
                    )
                kwargs[field] = value
            else:
                kwargs[field] = bool(value)
        return cls(**kwargs)


#: Known-vendor overrides as ``(host substring, flags)`` pairs, matched
#: against the request base URL in order. The sidecar's provider *kinds* are
#: generic ("openai_compat"), so the host is the only reliable vendor
#: signal. Entries pin divergences observed against the live vendor;
#: anything not listed runs on reference defaults. New divergences land here
#: as data — adding a vendor must not touch request-building or parsing.
PROVIDER_COMPAT_HOSTS: list[tuple[str, OpenAICompatFlags]] = [
    # DeepSeek: reasoning arrives as ``reasoning_content`` and cache hits as
    # top-level ``prompt_cache_hit_tokens``. The tolerant defaults already
    # cover both — this entry pins that coverage as data so a future default
    # change cannot silently regress it.
    (
        "api.deepseek.com",
        OpenAICompatFlags(
            reasoning_delta_fields=("reasoning_content", "reasoning"),
            cached_tokens_fields=(
                "prompt_cache_hit_tokens",
                "prompt_tokens_details.cached_tokens",
            ),
        ),
    ),
    # Moonshot (kimi-k2.5/k2.6/k2.7-code/k3): thinking models REJECT an
    # explicit ``temperature`` (fixed 1.0 thinking / 0.6 non-thinking, any
    # other value → HTTP 400 ``invalid temperature`` — platform.kimi.ai
    # model-parameter reference, reproduced live by vercel/ai#19543) and
    # reject ``reasoning_effort`` on every k2.x model (only k3 accepts
    # low/high/max). Both flags flip off for the host; a host that knows it
    # drives k3 can still pass either field explicitly — host extra kwargs
    # always win over flags. Reasoning arrives as ``reasoning_content``
    # (default covers it; pinned as data). Doc-verified 2026-08-30 against
    # platform.kimi.ai/docs/api/models-overview; live-key run pending.
    (
        "api.moonshot.cn",
        OpenAICompatFlags(
            supports_temperature=False,
            supports_reasoning_effort=False,
            reasoning_delta_fields=("reasoning_content", "reasoning"),
        ),
    ),
    (
        "api.moonshot.ai",
        OpenAICompatFlags(
            supports_temperature=False,
            supports_reasoning_effort=False,
            reasoning_delta_fields=("reasoning_content", "reasoning"),
        ),
    ),
    # OpenRouter normalizes every upstream reasoning shape into the
    # ``reasoning`` delta field (docs: "reasoning tokens will appear in the
    # ``reasoning`` field"; SDK ChatStreamDelta.reasoning), with
    # ``reasoning_details`` as the structured companion. Pin ``reasoning``
    # first so the normalized path wins; ``reasoning_content`` stays as the
    # pass-through fallback for upstreams OpenRouter does not rewrite.
    # Doc-verified 2026-08-30 against openrouter.ai/docs reasoning-tokens
    # guide; live-verified 2026-08-31 (W5.4.3): deepseek/deepseek-r1 over the
    # gateway delivered thinking in `reasoning` + `reasoning_details` on the
    # raw wire, and the pinned order surfaced 965 reasoning chars plus the
    # answer text through the framework's normalized chunk stream.
    (
        "openrouter.ai",
        OpenAICompatFlags(
            reasoning_delta_fields=("reasoning", "reasoning_content"),
        ),
    ),
    # DashScope / Alibaba Model Studio compatible mode (Qwen3 thinking,
    # DeepSeek hosted): reasoning arrives as ``reasoning_content`` and
    # ``stream_options.include_usage`` is documented-supported, so the
    # reference defaults already fit — this entry pins that coverage as
    # data. Thinking is toggled by the nonstandard ``enable_thinking``
    # extra-body field; hosts opt in via explicit extra kwargs, which
    # always win over flags. Doc-verified 2026-08-30 against
    # help.aliyun.com/en/model-studio deep-thinking guide; live-key run
    # pending.
    (
        "dashscope.aliyuncs.com",
        OpenAICompatFlags(
            reasoning_delta_fields=("reasoning_content", "reasoning"),
        ),
    ),
    (
        "dashscope-intl.aliyuncs.com",
        OpenAICompatFlags(
            reasoning_delta_fields=("reasoning_content", "reasoning"),
        ),
    ),
]


def compat_for_base_url(base_url: str | None) -> OpenAICompatFlags | None:
    """First registry entry whose host substring appears in ``base_url``."""
    if not base_url:
        return None
    for host, flags in PROVIDER_COMPAT_HOSTS:
        if host in base_url:
            return flags
    return None
