"""Provider compatibility matrix (P2.1).

Every place a vendor's "OpenAI-compatible" endpoint diverges from the
reference API is data on ``OpenAICompatFlags``, not a branch in the
provider. Acceptance: adding a vendor means adding a flags entry — no
changes to request-building or stream-parsing code.
"""

from __future__ import annotations

import json

import pytest
from steerable_agent_runtime.llm import OpenAICompatProvider
from steerable_agent_runtime.llm.compat import (
    OpenAICompatFlags,
    compat_for_base_url,
)
from steerable_agent_runtime.llm.openai_compat import _parse_stream_chunk, _parse_usage


def _provider(**over) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        name=over.pop("name", "unit"),
        model=over.pop("model", "m"),
        base_url=over.pop("base_url", "http://localhost/v1"),
        **over,
    )


def _stream_body(provider: OpenAICompatProvider, **kw) -> dict:
    from steerable_agent_runtime.llm import LLMMessage

    return provider._build_body(
        messages=[LLMMessage.text_of("user", "hi")],
        tools=None,
        temperature=kw.get("temperature"),
        max_tokens=kw.get("max_tokens"),
        stream=True,
        extra={},
    )


# ---------------------------------------------------------------------------
# Defaults == reference OpenAI behavior (locks current wire bytes)
# ---------------------------------------------------------------------------


def test_default_flags_match_reference_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEERABLE_REASONING_EFFORT", "low")
    # deepseek-reasoner has a reasoning knob, so the env effort is sent.
    body = _stream_body(_provider(model="deepseek-reasoner"), temperature=0.5, max_tokens=64)
    assert body["stream_options"] == {"include_usage": True}
    assert body["max_tokens"] == 64
    assert "max_completion_tokens" not in body
    assert body["temperature"] == 0.5
    assert body["reasoning_effort"] == "low"


# ---------------------------------------------------------------------------
# Request-shape flags
# ---------------------------------------------------------------------------


def test_usage_in_streaming_disabled_omits_stream_options() -> None:
    provider = _provider(compat=OpenAICompatFlags(supports_usage_in_streaming=False))
    body = _stream_body(provider)
    assert "stream_options" not in body


def test_max_tokens_field_override() -> None:
    provider = _provider(compat=OpenAICompatFlags(max_tokens_field="max_completion_tokens"))
    body = _stream_body(provider, max_tokens=64)
    assert body["max_completion_tokens"] == 64
    assert "max_tokens" not in body


def test_reasoning_effort_unsupported_suppresses_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEERABLE_REASONING_EFFORT", "low")
    provider = _provider(
        model="deepseek-reasoner",
        compat=OpenAICompatFlags(supports_reasoning_effort=False),
    )
    body = _stream_body(provider)
    assert "reasoning_effort" not in body


def test_temperature_unsupported_suppresses_field() -> None:
    provider = _provider(
        default_temperature=0.7,
        compat=OpenAICompatFlags(supports_temperature=False),
    )
    body = _stream_body(provider, temperature=0.5)
    assert "temperature" not in body


# ---------------------------------------------------------------------------
# Response-shape flags
# ---------------------------------------------------------------------------


def test_reasoning_delta_fields_override_parsing() -> None:
    chunk = {"choices": [{"delta": {"thinking": "hmm"}, "finish_reason": None}]}
    # Reference fields do not see a `thinking` delta…
    assert _parse_stream_chunk(chunk).reasoning_delta is None
    # …but a vendor flagged for it does.
    flags = OpenAICompatFlags(reasoning_delta_fields=("thinking",))
    assert _parse_stream_chunk(chunk, compat=flags).reasoning_delta == "hmm"


def test_reasoning_delta_fields_narrowing_drops_known_field() -> None:
    chunk = {"choices": [{"delta": {"reasoning": "hmm"}, "finish_reason": None}]}
    flags = OpenAICompatFlags(reasoning_delta_fields=("reasoning_content",))
    assert _parse_stream_chunk(chunk, compat=flags).reasoning_delta is None


def test_cached_tokens_fields_override_parsing() -> None:
    usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
             "cache_hits": 7}
    assert _parse_usage(usage).cached_prompt_tokens == 0
    flags = OpenAICompatFlags(cached_tokens_fields=("cache_hits",))
    assert _parse_usage(usage, compat=flags).cached_prompt_tokens == 7


def test_cached_tokens_dotted_path() -> None:
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "total_tokens": 12,
        "details": {"cache": {"hits": 5}},
    }
    flags = OpenAICompatFlags(cached_tokens_fields=("details.cache.hits",))
    assert _parse_usage(usage, compat=flags).cached_prompt_tokens == 5


# ---------------------------------------------------------------------------
# Registry + from_dict
# ---------------------------------------------------------------------------


def test_registry_covers_documented_vendors() -> None:
    # The deepseek entry pins the divergences the parser historically
    # tolerated implicitly: reasoning_content deltas and top-level
    # prompt_cache_hit_tokens. Matched by base-URL host, like pi's
    # URL auto-detection.
    entry = compat_for_base_url("https://api.deepseek.com/v1")
    assert entry is not None
    assert "reasoning_content" in entry.reasoning_delta_fields
    assert "prompt_cache_hit_tokens" in entry.cached_tokens_fields
    # The reference API itself needs no entry.
    assert compat_for_base_url("https://api.openai.com/v1") is None
    openrouter = compat_for_base_url("https://openrouter.ai/api/v1")
    assert openrouter is not None
    assert openrouter.supports_usage_in_streaming is False
    assert "reasoning" in openrouter.reasoning_delta_fields


def test_from_dict_roundtrip_and_unknown_key_fails_loud() -> None:
    flags = OpenAICompatFlags.from_dict(
        {"supportsUsageInStreaming": False, "maxTokensField": "max_completion_tokens"}
    )
    assert flags.supports_usage_in_streaming is False
    assert flags.max_tokens_field == "max_completion_tokens"
    with pytest.raises(ValueError, match="unknown"):
        OpenAICompatFlags.from_dict({"supportsEverything": True})


# ---------------------------------------------------------------------------
# Acceptance (2.1.2): a new vendor is data only
# ---------------------------------------------------------------------------


def test_new_vendor_added_with_data_only() -> None:
    """A fictional vendor with three divergences is fully described by one
    flags entry — request body and stream parsing both honor it without any
    provider code change."""
    acme = OpenAICompatFlags(
        supports_usage_in_streaming=False,
        max_tokens_field="max_completion_tokens",
        reasoning_delta_fields=("thinking",),
        cached_tokens_fields=("usage_details.cache",),
    )
    provider = _provider(compat=acme)

    body = _stream_body(provider, max_tokens=32)
    assert "stream_options" not in body
    assert body["max_completion_tokens"] == 32

    chunk = {"choices": [{"delta": {"thinking": "hmm"}, "finish_reason": None}]}
    assert _parse_stream_chunk(chunk, compat=provider.compat).reasoning_delta == "hmm"

    usage_payload = {
        "prompt_tokens": 3,
        "completion_tokens": 1,
        "total_tokens": 4,
        "usage_details": {"cache": 2},
    }
    assert _parse_usage(usage_payload, compat=provider.compat).cached_prompt_tokens == 2


def test_explicit_extra_kwargs_still_win_over_flags() -> None:
    """A host-passed explicit field (extra kwargs) is never clobbered by the
    flags layer — flags only gate what the provider itself adds."""
    provider = _provider(compat=OpenAICompatFlags(max_tokens_field="max_completion_tokens"))
    from steerable_agent_runtime.llm import LLMMessage

    body = provider._build_body(
        messages=[LLMMessage.text_of("user", "hi")],
        tools=None,
        temperature=None,
        max_tokens=None,
        stream=True,
        extra={"max_tokens": 11},
    )
    assert json.dumps(body)  # serializable
    assert body["max_tokens"] == 11
    assert "max_completion_tokens" not in body
