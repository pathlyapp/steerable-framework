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

from dataclasses import dataclass, replace

#: Canonical reasoning-effort ordering, lowest to highest. Used to clamp a
#: requested effort to the nearest level a model actually supports (pi's
#: ``clampThinkingLevel`` counterpart).
REASONING_EFFORT_ORDER: tuple[str, ...] = ("minimal", "low", "medium", "high")

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


def _match(model: str | None) -> ModelInfo:
    """Longest-prefix match over custom + built-in tables (custom first)."""
    if not model:
        return _DEFAULT_INFO
    name = model.lower()
    best = _DEFAULT_INFO
    best_len = -1
    for info in (*_custom_infos, *MODEL_INFOS):
        if name.startswith(info.pattern) and len(info.pattern) > best_len:
            best, best_len = info, len(info.pattern)
    return best


def resolve_model_info(
    model: str | None,
    *,
    context_window_override: int | None = None,
) -> ModelInfo:
    """The capability descriptor for ``model``.

    ``context_window_override`` (a positive int) wins over the table — for
    local daemons whose effective window is the daemon's, not the model's.
    """
    info = _match(model)
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
    levels = _match(model).reasoning_levels
    if not levels or not effort:
        return None
    requested = effort.strip().lower()
    if requested in levels:
        return requested
    if requested not in REASONING_EFFORT_ORDER:
        for fallback in ("medium", "low", "high", "minimal"):
            if fallback in levels:
                return fallback
        return sorted(levels)[0]
    target = REASONING_EFFORT_ORDER.index(requested)
    return min(levels, key=lambda lv: abs(REASONING_EFFORT_ORDER.index(lv) - target))
