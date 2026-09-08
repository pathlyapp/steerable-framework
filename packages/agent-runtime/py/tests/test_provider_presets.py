"""Provider presets: vendor-documented optimal parameters as data.

Two layers under test: the registry matcher (``preset_for``) and the
request-building integration in ``OpenAICompatProvider._build_body``, where
the precedence rule is explicit caller field > host extra kwargs >
``default_temperature`` > preset > omitted — with compat flags still gating
what may be sent.
"""

from __future__ import annotations

import pytest
from steerable_agent_runtime.llm import (
    PROVIDER_PRESETS,
    LLMMessage,
    OpenAICompatFlags,
    OpenAICompatProvider,
    PresetEntry,
    ProviderPreset,
    describe_provider_presets,
    preset_for,
    register_provider_preset,
)
from steerable_agent_runtime.llm import presets as presets_module


def _build(
    model: str,
    base_url: str,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    default_temperature: float | None = None,
    compat: OpenAICompatFlags | None = None,
    extra: dict | None = None,
    preset: ProviderPreset | str = "auto",
) -> dict:
    provider = OpenAICompatProvider(
        name="t",
        model=model,
        base_url=base_url,
        default_temperature=default_temperature,
        compat=compat,
        preset=preset,  # type: ignore[arg-type]
    )
    return provider._build_body(
        messages=[LLMMessage.text_of("user", "hi")],
        tools=None,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
        extra=extra or {},
    )


# ---------------------------------------------------------------------------
# Registry matching
# ---------------------------------------------------------------------------


def test_preset_matches_model_leaf_across_hosts() -> None:
    """Sampling optima follow the weights: a gateway-prefixed model id still
    matches on the leaf, whatever host serves it."""
    assert preset_for("https://openrouter.ai/api/v1", "qwen/Qwen3-32B") == ProviderPreset(
        temperature=0.6, top_p=0.95, extra_body={"top_k": 20}
    )
    assert preset_for("http://127.0.0.1:11434/v1", "llama3.1:8b") == ProviderPreset(
        temperature=0.6, top_p=0.9
    )
    assert preset_for(None, "openai/gpt-oss-120b") is not None


def test_preset_reasoner_shadows_broader_deepseek_entry() -> None:
    """The reasoner class must NOT inherit the chat class's 0.0 — DeepSeek
    rejects a non-1.0 temperature on thinking models with a 400."""
    reasoner = preset_for("https://api.deepseek.com/v1", "deepseek-reasoner")
    assert reasoner is not None
    assert reasoner.temperature is None
    chat = preset_for("https://api.deepseek.com/v1", "deepseek-chat")
    assert chat is not None
    assert chat.temperature == 0.0
    # The shadow holds off the official host too (gateway-served r1).
    assert preset_for("https://openrouter.ai/api/v1", "deepseek/deepseek-r1").temperature is None  # type: ignore[union-attr]


def test_preset_no_match_returns_none() -> None:
    assert preset_for("https://api.openai.com/v1", "gpt-5") is None
    assert preset_for(None, None) is None


def test_preset_kill_switch(monkeypatch) -> None:
    monkeypatch.setenv("STEERABLE_PROVIDER_PRESETS", "0")
    assert preset_for("https://api.deepseek.com/v1", "deepseek-chat") is None


def test_register_provider_preset_custom_first(monkeypatch) -> None:
    monkeypatch.setattr(presets_module, "_custom_entries", [])
    custom = PresetEntry(None, "deepseek", ProviderPreset(temperature=0.2))
    register_provider_preset(custom)
    assert preset_for("https://api.deepseek.com/v1", "deepseek-chat").temperature == 0.2  # type: ignore[union-attr]


def test_preset_entry_requires_a_match_key() -> None:
    with pytest.raises(ValueError, match="at least one match key"):
        PresetEntry(None, None, ProviderPreset())


def test_describe_provider_presets_shape() -> None:
    described = describe_provider_presets()
    assert len(described) == len(PROVIDER_PRESETS)
    deepseek = next(d for d in described if d["modelPrefix"] == "deepseek" and d["host"])
    assert deepseek == {
        "host": "api.deepseek.com",
        "modelPrefix": "deepseek",
        "temperature": 0.0,
        "topP": None,
        "maxTokens": None,
        "reasoningEffort": None,
        "extraBody": None,
    }


# ---------------------------------------------------------------------------
# Request-building integration
# ---------------------------------------------------------------------------


def test_build_body_applies_preset_temperature() -> None:
    body = _build("deepseek-chat", "https://api.deepseek.com/v1")
    assert body["temperature"] == 0.0


def test_build_body_reasoner_sends_no_temperature() -> None:
    body = _build("deepseek-reasoner", "https://api.deepseek.com/v1")
    assert "temperature" not in body


def test_build_body_explicit_fields_win_over_preset() -> None:
    assert _build("deepseek-chat", "https://api.deepseek.com/v1", temperature=0.9)[
        "temperature"
    ] == 0.9
    assert _build(
        "deepseek-chat", "https://api.deepseek.com/v1", default_temperature=0.4
    )["temperature"] == 0.4
    # Host extra kwargs are the most explicit channel of all.
    assert _build(
        "deepseek-chat", "https://api.deepseek.com/v1", extra={"temperature": 1.3}
    )["temperature"] == 1.3


def test_build_body_preset_top_p_and_extra_body_fill_only_absent() -> None:
    body = _build("qwen3-32b", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert body["top_p"] == 0.95
    assert body["top_k"] == 20
    overridden = _build(
        "qwen3-32b",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        extra={"top_p": 0.8, "top_k": 40},
    )
    assert overridden["top_p"] == 0.8
    assert overridden["top_k"] == 40


def test_build_body_compat_gate_beats_preset() -> None:
    """A preset never revives a field the vendor's compat entry disables —
    Moonshot's fixed-temperature models are the canonical case."""
    moonshot = OpenAICompatFlags(supports_temperature=False)
    body = _build("glm-4.6", "https://api.z.ai/api/paas/v4", compat=moonshot)
    assert "temperature" not in body
    # top_p is not gated by a compat flag, so the GLM preset still lands.
    assert body["top_p"] == 0.95


def test_build_body_preset_reasoning_effort(monkeypatch) -> None:
    monkeypatch.delenv("STEERABLE_REASONING_EFFORT", raising=False)
    body = _build("gpt-oss-120b", "https://openrouter.ai/api/v1")
    assert body["reasoning_effort"] == "medium"
    # The env request is explicit and wins over the preset default.
    monkeypatch.setenv("STEERABLE_REASONING_EFFORT", "high")
    assert _build("gpt-oss-120b", "https://openrouter.ai/api/v1")["reasoning_effort"] == "high"


def test_build_body_preset_max_tokens_via_compat_field(monkeypatch) -> None:
    monkeypatch.setattr(presets_module, "_custom_entries", [])
    register_provider_preset(
        PresetEntry(None, "my-model", ProviderPreset(max_tokens=8192))
    )
    body = _build("my-model", "http://x/v1")
    assert body["max_tokens"] == 8192
    assert _build("my-model", "http://x/v1", max_tokens=1024)["max_tokens"] == 1024


def test_build_body_kill_switch_disables_application(monkeypatch) -> None:
    monkeypatch.setenv("STEERABLE_PROVIDER_PRESETS", "off")
    body = _build("deepseek-chat", "https://api.deepseek.com/v1")
    assert "temperature" not in body


# ---------------------------------------------------------------------------
# Wire parsing (from_dict / to_dict)
# ---------------------------------------------------------------------------


def test_from_dict_round_trip() -> None:
    preset = ProviderPreset(
        temperature=0.6, top_p=0.95, max_tokens=8192, reasoning_effort="medium",
        extra_body={"top_k": 20},
    )
    assert ProviderPreset.from_dict(preset.to_dict()) == preset


def test_from_dict_unknown_keys_fail_loud() -> None:
    with pytest.raises(ValueError, match="unknown provider-preset keys"):
        ProviderPreset.from_dict({"temprature": 0.6})


def test_from_dict_extra_body_must_be_object() -> None:
    with pytest.raises(TypeError, match="extraBody must be an object"):
        ProviderPreset.from_dict({"extraBody": [1, 2]})


def test_to_dict_omits_unset_fields() -> None:
    assert ProviderPreset().to_dict() == {}
    assert ProviderPreset(temperature=0.0).to_dict() == {"temperature": 0.0}


# ---------------------------------------------------------------------------
# Provider-level preset selection (auto / off / pinned)
# ---------------------------------------------------------------------------


def test_provider_preset_off_disables_application() -> None:
    body = _build("deepseek-chat", "https://api.deepseek.com/v1", preset="off")
    assert "temperature" not in body


def test_provider_preset_pinned_overrides_registry() -> None:
    """A pinned preset applies even where the registry would match nothing —
    and shadows the registry entry that would otherwise win."""
    pinned = ProviderPreset(temperature=0.42, extra_body={"top_k": 7})
    body = _build("some-unknown-model", "http://x/v1", preset=pinned)
    assert body["temperature"] == 0.42
    assert body["top_k"] == 7
    shadowed = _build("deepseek-chat", "https://api.deepseek.com/v1", preset=pinned)
    assert shadowed["temperature"] == 0.42  # not the registry's 0.0


def test_provider_preset_pinned_still_respects_compat_gate() -> None:
    pinned = ProviderPreset(temperature=0.42)
    body = _build(
        "m", "http://x/v1", preset=pinned,
        compat=OpenAICompatFlags(supports_temperature=False),
    )
    assert "temperature" not in body
