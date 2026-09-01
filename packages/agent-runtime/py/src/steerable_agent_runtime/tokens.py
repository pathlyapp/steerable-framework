"""Token estimation — CJK-aware heuristic with per-model calibration.

Used where a real tokenizer is unavailable (compaction pressure trigger).
Providers report true usage for the budget axis; this only needs to be
proportionally right so thresholds trip at the same relative fill level.

Base heuristic (parity with deeppath-agent's ``estimateTokens``):
- CJK unified ideographs + CJK punctuation + fullwidth forms ≈ 0.6 token/char
  (empirically close for modern BPE tokenizers on Chinese text);
- everything else ≈ 0.25 token/char (the classic ~4 chars/token rule);
- +8 per message for role/framing overhead.

Per-model calibration: ``MODEL_TOKEN_FACTORS`` maps a model-name prefix to a
multiplicative factor over the base estimate (first prefix match wins).
Defaults are conservative (1.0 = uncalibrated); measure with your provider's
``usage`` reports and ``register_model_factor`` the result — the factor is
``observed_tokens / base_estimate`` over a representative transcript sample.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence

from .llm import LLMMessage, TextPart
from .model_info import DEFAULT_CONTEXT_WINDOW, MODEL_INFOS, resolve_model_info

#: CJK char → token, everything else → token (TS parity: 0.6 / 0.25).
_CJK_FACTOR = 0.6
_OTHER_FACTOR = 0.25
_MESSAGE_OVERHEAD = 8
#: Flat per-image estimate (order of magnitude of a high-detail image tile
#: set; exact accounting is provider- and size-dependent). Shared by the
#: recording layer's per-item cap heuristic.
IMAGE_PART_TOKEN_ESTIMATE = 1024


def _is_cjk(code: int) -> bool:
    return (
        0x4E00 <= code <= 0x9FFF  # CJK unified ideographs
        or 0x3000 <= code <= 0x303F  # CJK punctuation
        or 0xFF00 <= code <= 0xFFEF  # fullwidth forms
    )


def estimate_text_tokens(text: str) -> int:
    """CJK-aware token estimate for a single string."""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ord(ch)))
    other = len(text) - cjk
    return math.ceil(cjk * _CJK_FACTOR + other * _OTHER_FACTOR)


#: model-name prefix → multiplicative calibration factor (first match wins).
#: 1.0 = base heuristic. Extend at runtime via ``register_model_factor``.
#:
#: ``deepseek`` 0.71: calibrated 2026-08-26 against production MySQL
#: (llmusagedaily ⋈ chatmessage, service='harness_loop', 6,605 single-model
#: user-day buckets, 99.8% modelId='default' → deepseek-v4). Aggregate
#: regression gave Σ(actual completionTokens) / Σ(base heuristic estimate)
#: = 0.708 — the base heuristic overestimates DeepSeek-tokenizer completion
#: tokens by ~41% on real CJK-heavy traffic, so compaction thresholds trip
#: earlier than intended. The cjk/other per-char split is NOT identifiable
#: from day-level aggregates (corr(cjk, other) = 0.88), so the correction is
#: applied as a single global factor; per-char refinement needs per-request
#: estimated/observed pairs (sidecar self-recording, see CORELOOP_TODO P3).
MODEL_TOKEN_FACTORS: dict[str, float] = {"deepseek": 0.71}


def register_model_factor(prefix: str, factor: float) -> None:
    """Calibrate estimates for models whose name starts with ``prefix``.

    ``factor`` = observed_provider_tokens / base_estimate, measured over a
    representative transcript. Must be positive.
    """
    if not prefix:
        raise ValueError("prefix must be non-empty")
    if factor <= 0:
        raise ValueError("factor must be positive")
    MODEL_TOKEN_FACTORS[prefix.lower()] = factor


def factor_for_model(model: str | None) -> float:
    """Calibration factor for ``model`` (longest matching prefix wins)."""
    if not model:
        return 1.0
    name = model.lower()
    best = ""
    factor = 1.0
    for prefix, f in MODEL_TOKEN_FACTORS.items():
        if name.startswith(prefix) and len(prefix) > len(best):
            best, factor = prefix, f
    return factor


def estimate_tokens(messages: Sequence[LLMMessage], model: str | None = None) -> int:
    """Estimate total tokens for a transcript, calibrated for ``model``.

    Tool-call arguments are serialized compactly (no spaces, no ASCII
    escaping) to match what actually goes over the wire.
    """
    total = 0
    for m in messages:
        total += _MESSAGE_OVERHEAD
        total += estimate_text_tokens(m.content_text)
        total += IMAGE_PART_TOKEN_ESTIMATE * sum(
            1 for part in m.content if not isinstance(part, TextPart)
        )
        if m.tool_calls:
            for call in m.tool_calls:
                total += estimate_text_tokens(call.name)
                args = json.dumps(
                    call.arguments or {}, ensure_ascii=False, separators=(",", ":")
                )
                total += estimate_text_tokens(args)
        # Match the wire: OpenRouter gets details XOR plaintext, not both.
        if m.reasoning_details:
            total += estimate_text_tokens(
                json.dumps(m.reasoning_details, ensure_ascii=False, separators=(",", ":"))
            )
        elif m.reasoning:
            total += estimate_text_tokens(m.reasoning)
    return math.ceil(total * factor_for_model(model))


#: model-name prefix → provider context window in tokens (longest match wins).
#: Derived from the structured capability table (``model_info.MODEL_INFOS``,
#: W6-8) so window/modality/reasoning/tool-format stay a single source of
#: truth. Unknown models fall back to ``DEFAULT_CONTEXT_WINDOW``.
#:
#: Caveat: for a LOCAL Ollama daemon the effective window is the daemon's
#: ``num_ctx`` (default 4096), not the model's native window — pass an
#: explicit ``maxContextTokens`` for local-model deployments.
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    info.pattern: info.context_window for info in MODEL_INFOS
}


def resolve_context_window(
    model: str | None,
    explicit: int | None = None,
    *,
    provider: str | None = None,
    base_url: str | None = None,
) -> int:
    """Context window for ``model``: explicit config wins, then the catalog
    (provider/base_url scoped when given), then the legacy table, then the
    conservative fallback.

    Compaction thresholds derive from this (``threshold_ratio * window``), so
    a fixed default would either waste small-window models or — as with the
    old fixed 60k against 131k models — compact far earlier than the provider
    requires.
    """
    return resolve_model_info(
        model,
        provider=provider,
        base_url=base_url,
        context_window_override=explicit,
    ).context_window
