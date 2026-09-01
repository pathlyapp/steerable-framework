"""Structured model capabilities (W6-8) — window / modality / reasoning / tool format.

This replaces the model-id heuristics scattered across the runtime: the static
prefix→window table, the env-only reasoning-effort knob, and implicit
assumptions about tool support. A single ordered table of ``ModelInfo``
(longest name-prefix match wins) is the source of truth; ``resolve_model_info``
returns the capability descriptor for a model, falling back to a conservative
default for unknown models.

The competitors' counterparts: codex ``ModelInfo`` (+ ETag-cached remote
catalog), dsh ``ExactModel``, pi ``Model.compat`` + ``clampThinkingLevel``.
Ours is a static built-in table (extendable at runtime via
``register_model_info``) — a remote catalog is a separate concern.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

_log = logging.getLogger(__name__)

#: Canonical reasoning-effort ordering, lowest to highest. Used to clamp a
#: requested effort to the nearest level a model actually supports (pi's
#: ``clampThinkingLevel`` counterpart).
REASONING_EFFORT_ORDER: tuple[str, ...] = ("minimal", "low", "medium", "high", "max")

#: Fallback context window for unknown models — the pre-calibration desktop
#: default. (Defined here so ``model_info`` has no import cycle with
#: ``tokens``; ``tokens`` re-exports it for backward compatibility.)
DEFAULT_CONTEXT_WINDOW = 60_000

#: Tool wire formats a model expects.
TOOL_FORMAT_OPENAI = "openai"
TOOL_FORMAT_ANTHROPIC = "anthropic"
TOOL_FORMAT_NONE = "none"


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Structured capability description for a model family.

    ``pattern`` is a lowercased name-prefix; the longest matching pattern wins
    (so ``deepseek-reasoner`` can differ from the broader ``deepseek``).
    ``modalities`` is the set of input modalities (``"text"``, ``"image"``).
    ``tool_format`` is the tool wire format (``TOOL_FORMAT_*``); ``"none"``
    means the model does not accept tool definitions. ``reasoning_levels`` is
    the set of supported reasoning-effort levels (a subset of
    ``REASONING_EFFORT_ORDER``); empty means the model has no reasoning knob.
    """

    pattern: str
    context_window: int
    modalities: frozenset[str]
    tool_format: str
    reasoning_levels: frozenset[str]

    @property
    def supports_tools(self) -> bool:
        return self.tool_format != TOOL_FORMAT_NONE

    @property
    def supports_vision(self) -> bool:
        return "image" in self.modalities


#: Built-in capability table. ``context_window`` values mirror the
#: authoritative table in deeppath-api ``app/core/models_config.py``
#: (ProviderModelEntry.context_window) — keep them in sync.
#:
#: Caveat: for a LOCAL Ollama daemon the effective window is the daemon's
#: ``num_ctx`` (default 4096), not the model's native window — pass an explicit
#: ``context_window_override`` for local-model deployments.
MODEL_INFOS: tuple[ModelInfo, ...] = (
    ModelInfo("deepseek-reasoner", 131_072, frozenset({"text"}), TOOL_FORMAT_OPENAI, frozenset({"low", "medium", "high"})),
    ModelInfo("deepseek", 131_072, frozenset({"text"}), TOOL_FORMAT_OPENAI, frozenset()),
    # GLM-5.3 / Flash: 1M context (OpenRouter + Z.AI). Compacting at the
    # old 202k window folded tool results on long Terminal-Bench tasks.
    ModelInfo("z-ai/glm", 1_048_576, frozenset({"text"}), TOOL_FORMAT_OPENAI, frozenset({"low", "high", "max"})),
    ModelInfo("glm-5", 1_048_576, frozenset({"text"}), TOOL_FORMAT_OPENAI, frozenset({"low", "high", "max"})),
    ModelInfo("glm", 1_048_576, frozenset({"text"}), TOOL_FORMAT_OPENAI, frozenset({"low", "high", "max"})),
    ModelInfo("gpt-oss", 131_072, frozenset({"text"}), TOOL_FORMAT_OPENAI, frozenset({"low", "medium", "high"})),
    ModelInfo("llama3", 131_072, frozenset({"text"}), TOOL_FORMAT_OPENAI, frozenset()),
    ModelInfo("qwen3", 129_024, frozenset({"text"}), TOOL_FORMAT_OPENAI, frozenset()),
    ModelInfo("qwen2.5", 131_072, frozenset({"text"}), TOOL_FORMAT_OPENAI, frozenset()),
    ModelInfo("kimi-k2", 262_144, frozenset({"text"}), TOOL_FORMAT_OPENAI, frozenset()),
    ModelInfo("minimax", 197_000, frozenset({"text"}), TOOL_FORMAT_OPENAI, frozenset()),
    ModelInfo("claude", 200_000, frozenset({"text", "image"}), TOOL_FORMAT_ANTHROPIC, frozenset()),
    ModelInfo("gpt-5", 200_000, frozenset({"text", "image"}), TOOL_FORMAT_OPENAI, frozenset({"minimal", "low", "medium", "high"})),
    ModelInfo("gpt-4", 128_000, frozenset({"text", "image"}), TOOL_FORMAT_OPENAI, frozenset()),
)

#: Conservative default for unknown models: text-only, OpenAI tool format, no
#: reasoning knob, the fallback window.
_DEFAULT_INFO = ModelInfo("", DEFAULT_CONTEXT_WINDOW, frozenset({"text"}), TOOL_FORMAT_OPENAI, frozenset())

#: Runtime-registered overrides, consulted before the built-in table.
_custom_infos: list[ModelInfo] = []


def register_model_info(info: ModelInfo) -> None:
    """Register (or override) a capability descriptor at runtime.

    Custom entries are matched before the built-in table, so a deployment can
    describe a fine-tune or a newly released model without a framework release.
    """
    if not info.pattern:
        raise ValueError("pattern must be non-empty")
    _custom_infos.append(info)


def _name_candidates(model: str) -> tuple[str, ...]:
    """Full id plus the last path segment.

    Harbor ``--model openai/z-ai/glm-5.3-flash`` becomes
    ``z-ai/glm-5.3-flash``. A gateway that forwards the whole string still
    matches ``glm-5`` / ``glm`` on the leaf.
    """
    name = model.lower()
    leaf = name.rsplit("/", 1)[-1]
    if leaf == name:
        return (name,)
    return (name, leaf)


def _match(model: str | None) -> ModelInfo:
    """Longest-prefix match over custom + built-in tables (custom first)."""
    if not model:
        return _DEFAULT_INFO
    best = _DEFAULT_INFO
    best_len = -1
    for cand in _name_candidates(model):
        for info in (*_custom_infos, *MODEL_INFOS):
            if cand.startswith(info.pattern) and len(info.pattern) > best_len:
                best, best_len = info, len(info.pattern)
    return best


#: Resolution observers (W5.2.3): called with (model, source) whenever
#: resolution falls through to the legacy prefix table or the conservative
#: default — the two paths that were silently wrong for years. The default
#: observer logs; hosts register their own to surface the event.
_resolution_observers: list[Any] = []


def register_resolution_observer(observer: Any) -> None:
    """Subscribe to fallback resolutions: ``observer(model, source)`` where
    source is ``"legacy_prefix"`` or ``"default"``."""
    _resolution_observers.append(observer)


def _notify_fallback(model: str, source: str) -> None:
    if not _resolution_observers:
        _log.info("model %r resolved via %s (no catalog hit)", model, source)
        return
    for observer in _resolution_observers:
        observer(model, source)


def _catalog_match(
    model: str, provider: str | None, base_url: str | None
) -> ModelInfo | None:
    from .model_resolve import catalog_provider_for_base_url, resolve_in_catalog

    hit = resolve_in_catalog(provider, model)
    if hit is None and base_url:
        # Wire provider is often a compat shim ("openai_compat") that says
        # nothing about the *serving* provider — the endpoint names it
        # (openrouter.ai → openrouter namespace).
        hit = resolve_in_catalog(catalog_provider_for_base_url(base_url), model)
    if hit is None:
        return None
    return ModelInfo(
        pattern=hit.key,
        context_window=hit.context_window,
        modalities=frozenset(hit.input_modalities),
        tool_format=hit.tool_format,
        reasoning_levels=frozenset(hit.reasoning_levels),
    )


def resolve_model_info(
    model: str | None,
    *,
    provider: str | None = None,
    base_url: str | None = None,
    context_window_override: int | None = None,
) -> ModelInfo:
    """The capability descriptor for ``model``.

    Resolution order (W5.2): runtime-registered overrides, then the bundled
    models.dev catalog (exact → provider-scoped → same-provider prefix),
    then the legacy prefix table (kept for what the catalog cannot know:
    local daemons, delisted legacy ids), then the conservative default.
    Both fallback tiers are observable via ``register_resolution_observer``.

    ``context_window_override`` (a positive int) wins over every table — for
    local daemons whose effective window is the daemon's, not the model's.
    """
    info: ModelInfo | None = None
    if model:
        name = model.lower()
        custom_best: ModelInfo | None = None
        custom_len = -1
        for custom in _custom_infos:
            if name.startswith(custom.pattern) and len(custom.pattern) > custom_len:
                custom_best, custom_len = custom, len(custom.pattern)
        if custom_best is not None:
            info = custom_best
        else:
            info = _catalog_match(model, provider, base_url)
            if info is None:
                info = _match(model)
                if info is _DEFAULT_INFO:
                    _notify_fallback(model, "default")
                else:
                    _notify_fallback(model, "legacy_prefix")
    if info is None:
        info = _DEFAULT_INFO
    if context_window_override and context_window_override > 0:
        info = replace(info, context_window=context_window_override)
    return info


def clamp_reasoning_effort(model: str | None, effort: str | None) -> str | None:
    """Clamp a requested reasoning effort to a level ``model`` supports.

    Returns ``None`` when the model has no reasoning knob (so the caller sends
    nothing rather than an unsupported parameter), or when no effort was
    requested. An unsupported-but-recognized level clamps to the nearest
    supported level by the canonical ordering; an unrecognized value falls
    back to a sane supported default.
    """
    levels = resolve_model_info(model).reasoning_levels
    if not levels or not effort:
        return None
    requested = effort.strip().lower()
    if requested in levels:
        return requested
    if requested not in REASONING_EFFORT_ORDER:
        for fallback in ("medium", "low", "high", "minimal"):
            if fallback in levels:
                return fallback
        return min(levels)
    target = REASONING_EFFORT_ORDER.index(requested)
    return min(levels, key=lambda lv: abs(REASONING_EFFORT_ORDER.index(lv) - target))
