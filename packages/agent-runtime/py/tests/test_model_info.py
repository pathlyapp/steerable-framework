"""Structured model capabilities (W6-8): ModelInfo table + resolution + clamping."""

from __future__ import annotations

import pytest
from steerable_agent_runtime import (
    MODEL_INFOS,
    ModelInfo,
    clamp_reasoning_effort,
    register_model_info,
    resolve_model_info,
)
from steerable_agent_runtime.model_info import (
    DEFAULT_CONTEXT_WINDOW,
    TOOL_FORMAT_NONE,
    _custom_infos,
    _resolution_observers,
    register_resolution_observer,
)


@pytest.fixture(autouse=True)
def _clean_custom_infos():
    snapshot = list(_custom_infos)
    observers = list(_resolution_observers)
    try:
        yield
    finally:
        _custom_infos.clear()
        _custom_infos.extend(snapshot)
        _resolution_observers.clear()
        _resolution_observers.extend(observers)


def test_longest_prefix_wins() -> None:
    # deepseek-reasoner is more specific than deepseek.
    assert resolve_model_info("deepseek-reasoner").reasoning_levels == frozenset(
        {"low", "medium", "high"}
    )
    assert resolve_model_info("deepseek-chat").reasoning_levels == frozenset()
    # case-insensitive
    assert resolve_model_info("Kimi-K2.6").context_window == 262_144


def test_unknown_model_falls_back_to_conservative_default() -> None:
    info = resolve_model_info("some-finetune-9b")
    assert info.context_window == DEFAULT_CONTEXT_WINDOW
    assert info.modalities == frozenset({"text"})
    assert info.supports_tools is True  # OpenAI-format default
    assert info.supports_vision is False
    assert info.reasoning_levels == frozenset()
    assert resolve_model_info(None).context_window == DEFAULT_CONTEXT_WINDOW


def test_context_window_override_wins() -> None:
    info = resolve_model_info("deepseek-v4", context_window_override=32_000)
    assert info.context_window == 32_000
    # ...but the rest of the descriptor is intact
    assert info.supports_tools is True


# -- W5.2: catalog-first resolution -------------------------------------------


def test_catalog_exact_hit_beats_legacy_prefix() -> None:
    # Legacy table says claude = 200k; the catalog knows sonnet-4-6 is 1M.
    info = resolve_model_info("anthropic/claude-sonnet-4-6")
    assert info.context_window == 1_000_000
    assert info.pattern == "anthropic/claude-sonnet-4-6"


def test_catalog_scoped_hit_via_base_url_gateway() -> None:
    # The eval deployment shape: wire provider openai-compatible, model id
    # namespaced by the upstream vendor, endpoint naming the real provider.
    info = resolve_model_info(
        "z-ai/glm-5.3-flash",
        provider="openai_compat",
        base_url="https://openrouter.ai/api/v1",
    )
    assert info.context_window == 1_310_720  # not the legacy 202,752


def test_legacy_prefix_fallback_is_observable() -> None:
    events: list[tuple[str, str]] = []
    register_resolution_observer(lambda model, source: events.append((model, source)))
    # ollama is a local daemon — no catalog presence by design.
    info = resolve_model_info("llama3.3")
    assert info.context_window == 131_072  # legacy table still serves it
    assert events == [("llama3.3", "legacy_prefix")]


def test_default_fallback_is_observable() -> None:
    events: list[tuple[str, str]] = []
    register_resolution_observer(lambda model, source: events.append((model, source)))
    resolve_model_info("some-finetune-9b")
    assert events == [("some-finetune-9b", "default")]


def test_custom_override_still_wins_over_catalog() -> None:
    register_model_info(
        ModelInfo("anthropic/claude-sonnet-4-6", 42_000, frozenset({"text"}), "openai", frozenset())
    )
    info = resolve_model_info("anthropic/claude-sonnet-4-6")
    assert info.context_window == 42_000


def test_derived_capability_properties() -> None:
    claude = resolve_model_info("claude-sonnet-4")
    assert claude.supports_vision is True
    assert claude.tool_format == "anthropic"
    gpt4 = resolve_model_info("gpt-4o")
    assert gpt4.supports_vision is True
    assert gpt4.supports_tools is True


def test_supports_tools_false_for_none_format() -> None:
    register_model_info(
        ModelInfo("embed-only", 8_192, frozenset({"text"}), TOOL_FORMAT_NONE, frozenset())
    )
    assert resolve_model_info("embed-only-1").supports_tools is False


def test_register_model_info_overrides_builtin() -> None:
    register_model_info(
        ModelInfo("deepseek", 999, frozenset({"text", "image"}), "openai", frozenset())
    )
    info = resolve_model_info("deepseek-chat")
    assert info.context_window == 999
    assert info.supports_vision is True


def test_register_model_info_rejects_empty_pattern() -> None:
    with pytest.raises(ValueError):
        register_model_info(
            ModelInfo("", 1000, frozenset({"text"}), "openai", frozenset())
        )


def test_clamp_reasoning_effort_passthrough_for_supported() -> None:
    assert clamp_reasoning_effort("deepseek-reasoner", "low") == "low"
    assert clamp_reasoning_effort("deepseek-reasoner", "HIGH") == "high"


def test_clamp_reasoning_effort_none_for_no_knob_model() -> None:
    # deepseek-chat has no reasoning knob — send nothing rather than an
    # unsupported parameter.
    assert clamp_reasoning_effort("deepseek-chat", "high") is None
    assert clamp_reasoning_effort("llama3-8b", "low") is None


def test_clamp_reasoning_effort_none_when_not_requested() -> None:
    assert clamp_reasoning_effort("deepseek-reasoner", None) is None
    assert clamp_reasoning_effort("deepseek-reasoner", "") is None


def test_clamp_reasoning_effort_clamps_to_nearest_supported() -> None:
    # gpt-5 supports minimal..high; deepseek-reasoner lacks "minimal" → clamps
    # up to "low" (nearest in the canonical ordering).
    assert clamp_reasoning_effort("deepseek-reasoner", "minimal") == "low"
    assert clamp_reasoning_effort("gpt-5", "minimal") == "minimal"


def test_clamp_reasoning_effort_unrecognized_value_falls_back() -> None:
    assert clamp_reasoning_effort("deepseek-reasoner", "ultra") == "medium"


def test_builtin_table_is_consistent() -> None:
    for info in MODEL_INFOS:
        assert info.pattern and info.pattern == info.pattern.lower()
        assert info.context_window > 0
        assert "text" in info.modalities
        assert info.reasoning_levels <= frozenset({"minimal", "low", "medium", "high"})
