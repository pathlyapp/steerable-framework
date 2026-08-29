"""Optional per-model pricing → cost attribution (W6-9).

The runtime already tracks token/step/tool budgets; this adds the missing
money axis. A static price table (USD per 1M tokens, input/output) maps a
model-name prefix to its rates; ``estimate_cost_usd`` converts a usage
accumulation into a dollar figure. Pricing is OPTIONAL and deployment-specific
— providers reprice, and a self-hosted/local model has no bill — so an unknown
model yields ``None`` (no fabricated number), and deployments override via
``register_model_price``.

Rates below are list prices captured 2026-08 and are intentionally
conservative; treat them as estimates, not invoices. The UI labels them as
such.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per 1M tokens for a model family (longest name-prefix match wins).

    ``input_per_mtok`` / ``output_per_mtok`` are the list rates. ``None`` for
    both means "no price known" — a local/self-hosted model — and is the
    conservative default.
    """

    pattern: str
    input_per_mtok: float
    output_per_mtok: float


#: Built-in price table (USD per 1M tokens). Ordered; longest prefix wins.
#: Local/self-hosted models (Ollama etc.) are deliberately absent → no cost.
MODEL_PRICES: tuple[ModelPrice, ...] = (
    ModelPrice("deepseek-reasoner", 0.55, 2.19),
    ModelPrice("deepseek", 0.27, 1.10),
    ModelPrice("gpt-5", 1.25, 10.00),
    ModelPrice("gpt-4", 2.50, 10.00),
    ModelPrice("claude", 3.00, 15.00),
    ModelPrice("kimi-k2", 0.60, 2.50),
    ModelPrice("qwen", 0.40, 1.20),
    ModelPrice("minimax", 0.30, 1.20),
)

#: Runtime-registered price overrides, consulted before the built-in table.
_custom_prices: list[ModelPrice] = []


def register_model_price(price: ModelPrice) -> None:
    """Register/override a price at runtime (deployment-specific repricing)."""
    if not price.pattern:
        raise ValueError("pattern must be non-empty")
    _custom_prices.append(price)


def price_for_model(model: str | None) -> ModelPrice | None:
    """The price entry for ``model`` (longest prefix match), or None."""
    if not model:
        return None
    name = model.lower()
    best: ModelPrice | None = None
    best_len = -1
    for price in (*_custom_prices, *MODEL_PRICES):
        if name.startswith(price.pattern) and len(price.pattern) > best_len:
            best, best_len = price, len(price.pattern)
    return best


def estimate_cost_usd(
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
) -> float | None:
    """Estimated USD cost for a usage accumulation, or None when unpriced.

    None (not 0.0) is deliberate: an unknown/local model has no bill, and
    surfacing $0.00 would falsely imply "measured free". Callers render None
    as "—" (no estimate), not zero.
    """
    price = price_for_model(model)
    if price is None:
        return None
    return (
        prompt_tokens * price.input_per_mtok + completion_tokens * price.output_per_mtok
    ) / 1_000_000
