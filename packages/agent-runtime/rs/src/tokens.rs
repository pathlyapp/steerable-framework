//! CJK-aware token estimation for compaction pressure.

use crate::types::{ContentPart, LLMMessage};

pub const IMAGE_PART_TOKEN_ESTIMATE: i64 = 1024;

fn is_cjk(ch: char) -> bool {
    matches!(ch as u32, 0x4E00..=0x9FFF | 0x3000..=0x303F | 0xFF00..=0xFFEF)
}

pub fn estimate_text_tokens(text: &str) -> i64 {
    let cjk = text.chars().filter(|ch| is_cjk(*ch)).count() as f64;
    let total = text.chars().count() as f64;
    (cjk * 0.6 + (total - cjk) * 0.25).ceil() as i64
}

pub fn factor_for_model(model: &str) -> f64 {
    if model.to_ascii_lowercase().starts_with("deepseek") {
        0.71
    } else {
        1.0
    }
}

pub fn estimate_tokens(messages: &[LLMMessage], model: &str) -> i64 {
    let mut total = 0;
    for message in messages {
        total += 8 + estimate_text_tokens(&message.content_text());
        total += message
            .content
            .iter()
            .filter(|part| matches!(part, ContentPart::Image { .. }))
            .count() as i64
            * IMAGE_PART_TOKEN_ESTIMATE;
        for call in &message.tool_calls {
            total += estimate_text_tokens(&call.name);
            total += estimate_text_tokens(&call.arguments.to_string());
        }
        if let Some(details) = &message.reasoning_details {
            total += estimate_text_tokens(&details.to_string());
        } else if let Some(reasoning) = &message.reasoning {
            total += estimate_text_tokens(reasoning);
        }
    }
    (total as f64 * factor_for_model(model)).ceil() as i64
}

pub fn resolve_context_window(model: &str, explicit: Option<i64>, provider: Option<&str>) -> i64 {
    explicit
        .or_else(|| {
            crate::model_resolve::resolve_in_catalog(provider, model)
                .or_else(|| crate::model_resolve::resolve_leaf_cross_provider(model))
                .map(|hit| hit.context_window)
        })
        .unwrap_or(131_072)
}
