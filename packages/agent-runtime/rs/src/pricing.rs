//! Optional static list-price estimates for accumulated provider usage.

#[derive(Clone, Debug, PartialEq)]
pub struct ModelPrice {
    pub pattern: &'static str,
    pub input_per_mtok: f64,
    pub output_per_mtok: f64,
}

pub const MODEL_PRICES: &[ModelPrice] = &[
    ModelPrice {
        pattern: "deepseek-reasoner",
        input_per_mtok: 0.55,
        output_per_mtok: 2.19,
    },
    ModelPrice {
        pattern: "deepseek",
        input_per_mtok: 0.27,
        output_per_mtok: 1.10,
    },
    ModelPrice {
        pattern: "gpt-5",
        input_per_mtok: 1.25,
        output_per_mtok: 10.00,
    },
    ModelPrice {
        pattern: "gpt-4",
        input_per_mtok: 2.50,
        output_per_mtok: 10.00,
    },
    ModelPrice {
        pattern: "claude",
        input_per_mtok: 3.00,
        output_per_mtok: 15.00,
    },
    ModelPrice {
        pattern: "kimi-k2",
        input_per_mtok: 0.60,
        output_per_mtok: 2.50,
    },
    ModelPrice {
        pattern: "qwen",
        input_per_mtok: 0.40,
        output_per_mtok: 1.20,
    },
    ModelPrice {
        pattern: "minimax",
        input_per_mtok: 0.30,
        output_per_mtok: 1.20,
    },
];

pub fn price_for_model(model: &str) -> Option<&'static ModelPrice> {
    let model = model.to_ascii_lowercase();
    MODEL_PRICES
        .iter()
        .filter(|price| model.starts_with(price.pattern))
        .max_by_key(|price| price.pattern.len())
}

pub fn estimate_cost_usd(model: &str, prompt_tokens: i64, completion_tokens: i64) -> Option<f64> {
    let price = price_for_model(model)?;
    Some(
        (prompt_tokens as f64 * price.input_per_mtok
            + completion_tokens as f64 * price.output_per_mtok)
            / 1_000_000.0,
    )
}
