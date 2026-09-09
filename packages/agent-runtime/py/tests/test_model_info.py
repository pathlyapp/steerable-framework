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


def test_clamp_reasoning_effort_glm_supports_max() -> None:
    assert clamp_reasoning_effort("z-ai/glm-5.3-flash", "max") == "max"
    assert clamp_reasoning_effort("z-ai/glm-5.3-flash", "high") == "high"
    # Harbor leaf id and a gateway that forwards the full openai/z-ai/... string.
    assert clamp_reasoning_effort("glm-5.3-flash", "max") == "max"
    assert clamp_reasoning_effort("openai/z-ai/glm-5.3-flash", "max") == "max"
    assert resolve_model_info("glm-5.3-flash").context_window == 1_048_576
    assert resolve_model_info("z-ai/glm-5.3-flash").context_window == 1_048_576
    assert resolve_model_info("openai/z-ai/glm-5.3-flash").context_window == 1_048_576


def test_clamp_reasoning_effort_xhigh_semantics() -> None:
    # xhigh sits between high and max (pi's ThinkingLevel vocabulary). A
    # model topping out at high clamps xhigh down to high...
    assert clamp_reasoning_effort("deepseek-reasoner", "xhigh") == "high"
    # ...and a GLM-style {low, high, max} knob resolves the high/max tie
    # deterministically toward the lower level (never over-promising).
    assert clamp_reasoning_effort("z-ai/glm-5.3-flash", "xhigh") == "high"


def test_clamp_reasoning_effort_xhigh_passthrough_when_supported() -> None:
    register_model_info(
        ModelInfo(
            "xhigh-knob-model",
            128_000,
            frozenset({"text"}),
            "openai",
            frozenset({"low", "high", "xhigh"}),
        )
    )
    assert clamp_reasoning_effort("xhigh-knob-model", "xhigh") == "xhigh"
    assert clamp_reasoning_effort("xhigh-knob-model", "max") == "xhigh"
    assert clamp_reasoning_effort("xhigh-knob-model", "xhigh", strict=True) == "xhigh"


def test_clamp_strict_raises_xhigh_not_supported() -> None:
    from steerable_agent_runtime import ReasoningEffortUnsupported

    with pytest.raises(ReasoningEffortUnsupported) as excinfo:
        clamp_reasoning_effort("deepseek-reasoner", "xhigh", strict=True)
    assert excinfo.value.reason == "level_not_supported"


def test_builtin_table_is_consistent() -> None:
    for info in MODEL_INFOS:
        assert info.pattern and info.pattern == info.pattern.lower()
        assert info.context_window > 0
        assert "text" in info.modalities
        assert info.reasoning_levels <= frozenset(
            {"minimal", "low", "medium", "high", "xhigh", "max"}
        )


# ---------------------------------------------------------------------------
# Cross-provider leaf join (EVALS 2.5.22) + gateway registry + strict clamp
# ---------------------------------------------------------------------------


def test_leaf_join_fills_reasoning_levels_for_gateway_namespaced_ids() -> None:
    # The 2.5.22 repro: the gateway's own "openai/" namespace has no catalog
    # entries, so every same-provider tier missed and the env effort was
    # silently dropped. The leaf join finds qwen3.8-27b's catalog entries and
    # unions their levels into the legacy qwen3 descriptor.
    info = resolve_model_info(
        "openai/qwen/qwen3.8-27b",
        provider="openai",
        base_url="http://localhost:8317/v1",
    )
    # Upstream models.dev lists qwen3.8-27b's knob as low/medium/xhigh
    # (xhigh is the level Artificial Analysis's run used).
    assert info.reasoning_levels == frozenset({"low", "medium", "xhigh"})
    # Legacy keeps the window (hand-owned table) — the join only adds levels.
    assert info.context_window == 129_024
    assert (
        clamp_reasoning_effort(
            "openai/qwen/qwen3.8-27b",
            "medium",
            provider="openai",
            base_url="http://localhost:8317/v1",
            strict=True,
        )
        == "medium"
    )


def test_leaf_join_is_exact_not_prefix() -> None:
    # "qwen3.8-27b" must not claim "qwen3.8-27b-instruct"-style near-misses;
    # a leaf with no exact catalog entry falls through to legacy/default.
    from steerable_agent_runtime.model_resolve import resolve_leaf_cross_provider

    assert resolve_leaf_cross_provider("openai/qwen/qwen3.8-27b") is not None
    assert resolve_leaf_cross_provider("openai/qwen/qwen3.8-27b-instruct") is None


def test_leaf_join_strips_endpoint_modifiers() -> None:
    # OpenRouter-style variant suffixes (`:free` / `:batch` / `:extended`)
    # change pricing or throughput, never the capability descriptor — the
    # leaf join matches the canonical id.
    from steerable_agent_runtime.model_resolve import resolve_leaf_cross_provider

    assert resolve_leaf_cross_provider("z-ai/glm-5.3-flash:batch") is not None
    assert resolve_leaf_cross_provider("openai/qwen/qwen3.8-27b:free") is not None
    assert resolve_leaf_cross_provider("openai/qwen/qwen3.8-27b-instruct:batch") is None


def test_leaf_join_notified_as_catalog_leaf() -> None:
    seen: list[tuple[str, str]] = []
    register_resolution_observer(lambda model, source: seen.append((model, source)))
    resolve_model_info("openai/qwen/qwen3.8-27b")
    assert ("openai/qwen/qwen3.8-27b", "catalog_leaf") in seen


def test_leaf_join_preserves_hand_owned_levels_via_union() -> None:
    # models.dev lists GLM-5.3-Flash as low/high everywhere; the legacy table
    # carries the wire-verified "max" (Z.AI default, TB uses it). The union
    # keeps max available even when the serving namespace answers first.
    info = resolve_model_info(
        "z-ai/glm-5.3-flash", base_url="https://api.z.ai/api/coding/paas/v4"
    )
    assert "max" in info.reasoning_levels
    assert (
        clamp_reasoning_effort(
            "z-ai/glm-5.3-flash",
            "max",
            base_url="https://api.z.ai/api/coding/paas/v4",
            strict=True,
        )
        == "max"
    )


def test_gateway_registry_wins_by_exact_id() -> None:
    from steerable_agent_runtime.model_info import (
        _gateway_infos,
        clear_gateway_models,
        register_gateway_models,
    )

    snapshot = dict(_gateway_infos)
    try:
        register_gateway_models(
            [
                ModelInfo(
                    "openai/qwen/qwen3.8-27b",
                    500_000,
                    frozenset({"text"}),
                    "openai",
                    frozenset({"low", "medium", "high"}),
                )
            ]
        )
        # Exact id hit: the gateway-advertised window and levels win over
        # every static tier.
        info = resolve_model_info("openai/qwen/qwen3.8-27b")
        assert info.context_window == 500_000
        assert info.reasoning_levels == frozenset({"low", "medium", "high"})
        # Case-insensitive id match…
        assert resolve_model_info("OpenAI/Qwen/Qwen3.8-27b").context_window == 500_000
        # …but never a prefix match: a live listing names concrete
        # deployments, not families.
        assert (
            resolve_model_info("openai/qwen/qwen3.8-27b-instruct").context_window
            != 500_000
        )
    finally:
        clear_gateway_models()
        _gateway_infos.update(snapshot)


def test_clamp_strict_raises_unknown_model() -> None:
    from steerable_agent_runtime import ReasoningEffortUnsupported

    with pytest.raises(ReasoningEffortUnsupported) as excinfo:
        clamp_reasoning_effort("totally-unknown-model", "high", strict=True)
    assert excinfo.value.reason == "unknown_model"
    assert "no catalog tier" in str(excinfo.value)


def test_clamp_strict_raises_no_reasoning_knob() -> None:
    from steerable_agent_runtime import ReasoningEffortUnsupported

    with pytest.raises(ReasoningEffortUnsupported) as excinfo:
        clamp_reasoning_effort("deepseek-chat", "low", strict=True)
    assert excinfo.value.reason == "no_reasoning_knob"


def test_clamp_strict_raises_level_not_supported() -> None:
    from steerable_agent_runtime import ReasoningEffortUnsupported

    with pytest.raises(ReasoningEffortUnsupported) as excinfo:
        clamp_reasoning_effort("deepseek-reasoner", "max", strict=True)
    assert excinfo.value.reason == "level_not_supported"
    assert "low" in str(excinfo.value) and "high" in str(excinfo.value)


def test_clamp_strict_raises_unknown_level() -> None:
    from steerable_agent_runtime import ReasoningEffortUnsupported

    with pytest.raises(ReasoningEffortUnsupported) as excinfo:
        clamp_reasoning_effort("deepseek-reasoner", "hige", strict=True)
    assert excinfo.value.reason == "unknown_level"


def test_clamp_non_strict_behavior_unchanged() -> None:
    # The default stays clamp-or-None for callers that probe capabilities
    # (settings UIs rendering a preview, not sending a request).
    assert clamp_reasoning_effort("deepseek-chat", "low") is None
    assert clamp_reasoning_effort("totally-unknown-model", "low") is None
    assert clamp_reasoning_effort("deepseek-reasoner", "max") == "high"
    assert clamp_reasoning_effort("deepseek-reasoner", None, strict=True) is None
