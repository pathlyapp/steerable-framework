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

from .llm import LLMMessage

#: CJK char → token, everything else → token (TS parity: 0.6 / 0.25).
_CJK_FACTOR = 0.6
_OTHER_FACTOR = 0.25
_MESSAGE_OVERHEAD = 8


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
        total += estimate_text_tokens(m.content or "")
        if m.tool_calls:
            for call in m.tool_calls:
                total += estimate_text_tokens(call.name)
                args = json.dumps(
                    call.arguments or {}, ensure_ascii=False, separators=(",", ":")
                )
                total += estimate_text_tokens(args)
    return math.ceil(total * factor_for_model(model))
