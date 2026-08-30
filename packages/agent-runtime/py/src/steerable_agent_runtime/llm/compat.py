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
        key_map = {
            "supportsUsageInStreaming": "supports_usage_in_streaming",
            "maxTokensField": "max_tokens_field",
            "supportsReasoningEffort": "supports_reasoning_effort",
            "supportsTemperature": "supports_temperature",
            "reasoningDeltaFields": "reasoning_delta_fields",
            "cachedTokensFields": "cached_tokens_fields",
        }
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
]


def compat_for_base_url(base_url: str | None) -> OpenAICompatFlags | None:
    """First registry entry whose host substring appears in ``base_url``."""
    if not base_url:
        return None
    for host, flags in PROVIDER_COMPAT_HOSTS:
        if host in base_url:
            return flags
    return None
