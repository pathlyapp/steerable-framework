"""Structured model capabilities (W6-8) — window / modality / reasoning / tool format.

This replaces the model-id heuristics scattered across the runtime: the static
prefix→window table, the env-only reasoning-effort knob, and implicit
assumptions about tool support. A single ordered table of ``ModelInfo``
(longest name-prefix match wins) is the source of truth; ``resolve_model_info``
returns the capability descriptor for a model, falling back to a conservative
default for unknown models.

The competitors' counterparts: codex ``ModelInfo`` (+ ETag-cached remote
catalog), dsh ``ExactModel``, pi ``Model.compat`` + ``clampThinkingLevel``.
Ours layers: runtime-registered overrides, the gateway's live listing
(``gateway_catalog``, installed here via ``register_gateway_models``), the
bundled models.dev catalog, then the legacy prefix table.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Iterable

_log = logging.getLogger(__name__)

#: Canonical reasoning-effort ordering, lowest to highest. Used to clamp a
#: requested effort to the nearest level a model actually supports (pi's
#: ``clampThinkingLevel`` counterpart). ``xhigh`` sits between ``high`` and
#: ``max`` (pi's ``ThinkingLevel`` vocabulary): extended levels are opt-in —
#: a catalog or legacy entry must list them explicitly, so a strict request
#: for ``xhigh`` errors on models whose knob tops out at ``high`` instead of
#: silently running there.
REASONING_EFFORT_ORDER: tuple[str, ...] = (
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

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


#: Gateway-discovered capabilities (``gateway_catalog.merge_with_catalog``
#: rows), keyed by lowercased gateway id and matched EXACTLY — a live listing
#: names concrete deployments, not families, so prefix matching would let
#: ``qwen3.8-27b`` claim ``qwen3.8-27b-instruct``'s entry. Replaced wholesale
#: on each successful fetch: the live listing is authoritative for its ids.
_gateway_infos: dict[str, ModelInfo] = {}


def register_gateway_models(infos: Iterable[ModelInfo]) -> None:
    """Install the gateway's live listing into the resolution path.

    Each info's ``pattern`` must be the lowercased gateway id verbatim.
    Called by the sidecar after every successful ``GET /models`` refresh, so
    the request path (window budgeting, reasoning clamp) sees the same
    capabilities the model picker displayed.
    """
    _gateway_infos.clear()
    for info in infos:
        if not info.pattern:
            raise ValueError(
                "gateway model info requires a non-empty pattern (the gateway id)"
            )
        _gateway_infos[info.pattern] = info


def clear_gateway_models() -> None:
    """Drop the installed gateway listing (tests, gateway reconfigure)."""
    _gateway_infos.clear()


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
#: default — the two paths that were silently wrong for years — and whenever
#: a namespaced id resolves through the cross-provider leaf join
#: (``catalog_leaf``): a hit, but a heuristic one, and invisible joins are
#: how EVALS 2.5.22 stayed undiagnosed. The default observer logs; hosts
#: register their own to surface the event.
_resolution_observers: list[Any] = []


def register_resolution_observer(observer: Any) -> None:
    """Subscribe to non-exact resolutions: ``observer(model, source)`` where
    source is ``"legacy_prefix"``, ``"default"``, or ``"catalog_leaf"``."""
    _resolution_observers.append(observer)


def _notify_fallback(model: str, source: str) -> None:
    if not _resolution_observers:
        _log.info("model %r resolved via %s (no catalog hit)", model, source)
        return
    for observer in _resolution_observers:
        observer(model, source)


def _catalog_hit(
    model: str, provider: str | None, base_url: str | None
) -> tuple[Any, bool] | None:
    """The catalog's claim on ``model`` — ``(CatalogHit, derived)`` or None.

    Tiers: exact → provider-scoped → same-provider prefix (all in
    ``model_resolve.resolve_in_catalog``), then for namespaced ids the leaf
    re-resolved within the serving/wire namespace, then the cross-provider
    leaf join. ``derived`` marks hits reached through a *rewritten* query
    (the leaf, not the id as given) — the caller merges those with the
    legacy table differently from deployment-attributed full-id hits.
    Import is deferred to keep the module cycle-free.
    """
    from .model_resolve import (
        catalog_provider_for_base_url,
        resolve_in_catalog,
        resolve_leaf_cross_provider,
    )

    hit = resolve_in_catalog(provider, model)
    serving = catalog_provider_for_base_url(base_url) if base_url else None
    if hit is None and serving:
        # Wire provider is often a compat shim ("openai_compat") that says
        # nothing about the *serving* provider — the endpoint names it
        # (openrouter.ai → openrouter namespace).
        hit = resolve_in_catalog(serving, model)
    if hit is not None:
        return (hit, False)
    if "/" not in model:
        return None
    # Gateway-namespaced ids ("openai/qwen/qwen3.8-27b" on a private
    # gateway) miss every same-provider tier: the wire namespace is not
    # the serving namespace. Re-resolve the bare leaf within the serving
    # (then wire) provider's namespace, then join cross-provider on
    # exact leaf equality (EVALS 2.5.22). Bare ids skip this tier — the
    # legacy table stays their home (local daemons whose effective
    # window is the daemon's, not the model's).
    leaf = model.rsplit("/", 1)[-1]
    for namespace in (serving, provider):
        if namespace:
            hit = resolve_in_catalog(namespace, leaf)
            if hit is not None:
                return (hit, True)
    hit = resolve_leaf_cross_provider(model)
    return (hit, True) if hit is not None else None


def _info_from_hit(hit: Any, *, reasoning_levels: frozenset[str] | None = None) -> ModelInfo:
    return ModelInfo(
        pattern=hit.key,
        context_window=hit.context_window,
        modalities=frozenset(hit.input_modalities),
        tool_format=hit.tool_format,
        reasoning_levels=(
            reasoning_levels
            if reasoning_levels is not None
            else frozenset(hit.reasoning_levels)
        ),
    )


def resolve_model_info(
    model: str | None,
    *,
    provider: str | None = None,
    base_url: str | None = None,
    context_window_override: int | None = None,
) -> ModelInfo:
    """The capability descriptor for ``model``.

    Resolution order (W5.2): runtime-registered overrides, then the
    gateway's live listing (exact id), then the bundled models.dev catalog
    (exact → provider-scoped → same-provider prefix → cross-provider leaf
    for gateway-namespaced ids), then the legacy prefix table (kept for what
    the catalog cannot know: local daemons, delisted legacy ids), then the
    conservative default. When the legacy table also claims the model, its
    reasoning levels UNION into the catalog result — the legacy table
    carries hand-verified knob vocabularies (Z.AI GLM's ``max``) that
    models.dev has not recorded, and a level the deployment truly lacks
    fails loud at the wire rather than silently here. For *derived* hits
    (reached via the leaf, not the id as given) the legacy descriptor
    otherwise wins — its windows mirror deeppath-api's authoritative table.
    The cross-provider leaf join, legacy table, and default are observable
    via ``register_resolution_observer``.

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
            info = _gateway_infos.get(name)
            if info is None:
                found = _catalog_hit(model, provider, base_url)
                if found is not None:
                    hit, derived = found
                    legacy = _match(model)
                    if legacy is not _DEFAULT_INFO:
                        union = frozenset(
                            legacy.reasoning_levels | frozenset(hit.reasoning_levels)
                        )
                        info = (
                            replace(legacy, reasoning_levels=union)
                            if derived
                            else replace(_info_from_hit(hit), reasoning_levels=union)
                        )
                    else:
                        info = _info_from_hit(hit)
                    if hit.source == "leaf":
                        _notify_fallback(model, "catalog_leaf")
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


class ReasoningEffortUnsupported(ValueError):
    """An explicit reasoning-effort request the catalog cannot honor.

    Raised by ``clamp_reasoning_effort(..., strict=True)`` where the legacy
    path silently sent nothing (EVALS 2.5.22). ``reason`` is one of:

    - ``unknown_model`` — no override, gateway listing, catalog tier, or
      legacy prefix claims the id, so support cannot even be verified.
    - ``no_reasoning_knob`` — the resolved entry lists no reasoning levels.
    - ``level_not_supported`` — the level is recognized but absent from the
      entry's list.
    - ``unknown_level`` — the request is not a reasoning-effort level at all
      (a typo would otherwise clamp to an arbitrary level).
    """

    def __init__(
        self,
        model: str | None,
        requested: str,
        supported: Iterable[str],
        reason: str,
    ) -> None:
        self.model = model
        self.requested = requested
        self.supported = tuple(supported)
        self.reason = reason
        super().__init__(self._format())

    def _format(self) -> str:
        ordered = sorted(
            self.supported,
            key=lambda lv: (
                REASONING_EFFORT_ORDER.index(lv)
                if lv in REASONING_EFFORT_ORDER
                else len(REASONING_EFFORT_ORDER)
            ),
        )
        if self.reason == "unknown_model":
            return (
                f"reasoning effort {self.requested!r} was requested for "
                f"{self.model!r}, which no catalog tier, gateway listing, or "
                "legacy table knows — support cannot be verified and silently "
                "dropping the knob is how misconfigured runs stay invisible. "
                "Register the model (register_model_info / the gateway's "
                "listing) or unset the effort request."
            )
        if self.reason == "no_reasoning_knob":
            return (
                f"{self.model!r} has no reasoning-effort knob in the resolved "
                f"catalog entry; the requested effort {self.requested!r} would "
                "be silently dropped. Unset the request or choose a reasoning "
                "model."
            )
        if self.reason == "level_not_supported":
            return (
                f"{self.model!r} does not support reasoning effort "
                f"{self.requested!r}; supported levels: {', '.join(ordered)} "
                "(or unset the request)."
            )
        return (
            f"{self.requested!r} is not a reasoning-effort level; known "
            f"levels: {', '.join(REASONING_EFFORT_ORDER)}."
        )


def clamp_reasoning_effort(
    model: str | None,
    effort: str | None,
    *,
    provider: str | None = None,
    base_url: str | None = None,
    strict: bool = False,
) -> str | None:
    """Clamp a requested reasoning effort to a level ``model`` supports.

    Returns ``None`` when the model has no reasoning knob (so the caller sends
    nothing rather than an unsupported parameter), or when no effort was
    requested. An unsupported-but-recognized level clamps to the nearest
    supported level by the canonical ordering; an unrecognized value falls
    back to a sane supported default.

    ``provider``/``base_url`` feed catalog resolution (a gateway-namespaced
    id joins its serving namespace by leaf). With ``strict=True`` — the
    request-path default — an explicit request that cannot be honored raises
    ``ReasoningEffortUnsupported`` instead of returning ``None`` and sending
    nothing: unknown model, no reasoning knob, unsupported level, and
    unrecognized level all fail loud.
    """
    requested = (effort or "").strip().lower()
    if not requested:
        return None
    info = resolve_model_info(model, provider=provider, base_url=base_url)
    levels = info.reasoning_levels
    if strict:
        if not info.pattern:
            raise ReasoningEffortUnsupported(model, requested, (), "unknown_model")
        if not levels:
            raise ReasoningEffortUnsupported(model, requested, (), "no_reasoning_knob")
        if requested not in REASONING_EFFORT_ORDER:
            raise ReasoningEffortUnsupported(model, requested, levels, "unknown_level")
        if requested not in levels:
            raise ReasoningEffortUnsupported(
                model, requested, levels, "level_not_supported"
            )
    if not levels:
        return None
    if requested in levels:
        return requested
    if requested not in REASONING_EFFORT_ORDER:
        for fallback in ("medium", "low", "high", "minimal"):
            if fallback in levels:
                return fallback
        return min(levels)
    target = REASONING_EFFORT_ORDER.index(requested)
    # Nearest level by canonical distance; ties break toward the LOWER level
    # (conservative — never over-promise compute the request did not name),
    # which also keeps the choice deterministic across hash seeds.
    return min(
        levels,
        key=lambda lv: (
            abs(REASONING_EFFORT_ORDER.index(lv) - target),
            REASONING_EFFORT_ORDER.index(lv),
        ),
    )
