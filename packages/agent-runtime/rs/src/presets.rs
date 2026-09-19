//! Vendor-documented generation defaults keyed by endpoint and model family.

use serde_json::{json, Map, Value};

#[derive(Clone, Debug, Default, PartialEq)]
pub struct ProviderPreset {
    pub temperature: Option<f64>,
    pub top_p: Option<f64>,
    pub max_tokens: Option<i64>,
    pub reasoning_effort: Option<String>,
    pub extra_body: Map<String, Value>,
}

impl ProviderPreset {
    pub fn to_wire(&self) -> Value {
        let mut out = Map::new();
        if let Some(value) = self.temperature {
            out.insert("temperature".into(), json!(value));
        }
        if let Some(value) = self.top_p {
            out.insert("topP".into(), json!(value));
        }
        if let Some(value) = self.max_tokens {
            out.insert("maxTokens".into(), json!(value));
        }
        if let Some(value) = &self.reasoning_effort {
            out.insert("reasoningEffort".into(), json!(value));
        }
        if !self.extra_body.is_empty() {
            out.insert("extraBody".into(), Value::Object(self.extra_body.clone()));
        }
        Value::Object(out)
    }
}

#[derive(Clone, Debug)]
pub struct PresetEntry {
    pub host: Option<&'static str>,
    pub model_prefix: Option<&'static str>,
    pub preset: ProviderPreset,
}

fn preset(
    temperature: Option<f64>,
    top_p: Option<f64>,
    reasoning_effort: Option<&str>,
    extra_body: Value,
) -> ProviderPreset {
    ProviderPreset {
        temperature,
        top_p,
        max_tokens: None,
        reasoning_effort: reasoning_effort.map(str::to_string),
        extra_body: extra_body.as_object().cloned().unwrap_or_default(),
    }
}

pub fn provider_presets() -> Vec<PresetEntry> {
    vec![
        PresetEntry {
            host: Some("api.deepseek.com"),
            model_prefix: Some("deepseek-reasoner"),
            preset: ProviderPreset::default(),
        },
        PresetEntry {
            host: None,
            model_prefix: Some("deepseek-reasoner"),
            preset: ProviderPreset::default(),
        },
        PresetEntry {
            host: None,
            model_prefix: Some("deepseek-r1"),
            preset: ProviderPreset::default(),
        },
        PresetEntry {
            host: Some("api.deepseek.com"),
            model_prefix: Some("deepseek"),
            preset: preset(Some(0.0), None, None, json!({})),
        },
        PresetEntry {
            host: None,
            model_prefix: Some("deepseek"),
            preset: preset(Some(0.0), None, None, json!({})),
        },
        PresetEntry {
            host: None,
            model_prefix: Some("qwen3"),
            preset: preset(Some(0.6), Some(0.95), None, json!({"top_k": 20})),
        },
        PresetEntry {
            host: Some("api.z.ai"),
            model_prefix: Some("glm"),
            preset: preset(Some(1.0), Some(0.95), None, json!({})),
        },
        PresetEntry {
            host: Some("bigmodel.cn"),
            model_prefix: Some("glm"),
            preset: preset(Some(1.0), Some(0.95), None, json!({})),
        },
        PresetEntry {
            host: None,
            model_prefix: Some("glm-"),
            preset: preset(Some(1.0), Some(0.95), None, json!({})),
        },
        PresetEntry {
            host: None,
            model_prefix: Some("llama3"),
            preset: preset(Some(0.6), Some(0.9), None, json!({})),
        },
        PresetEntry {
            host: None,
            model_prefix: Some("llama-3"),
            preset: preset(Some(0.6), Some(0.9), None, json!({})),
        },
        PresetEntry {
            host: None,
            model_prefix: Some("llama-4"),
            preset: preset(Some(0.6), Some(0.9), None, json!({})),
        },
        PresetEntry {
            host: None,
            model_prefix: Some("gpt-oss"),
            preset: preset(Some(1.0), Some(1.0), Some("medium"), json!({})),
        },
        PresetEntry {
            host: None,
            model_prefix: Some("minimax"),
            preset: preset(Some(1.0), Some(0.95), None, json!({"top_k": 40})),
        },
    ]
}

fn enabled() -> bool {
    !matches!(
        std::env::var("STEERABLE_PROVIDER_PRESETS")
            .unwrap_or_else(|_| "1".into())
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "0" | "false" | "off" | "no"
    )
}

pub fn preset_for(base_url: &str, model: &str) -> Option<ProviderPreset> {
    if !enabled() {
        return None;
    }
    let url = base_url.to_ascii_lowercase();
    let leaf = model
        .rsplit('/')
        .next()
        .unwrap_or(model)
        .to_ascii_lowercase();
    provider_presets()
        .into_iter()
        .filter(|entry| {
            entry.host.is_none_or(|host| url.contains(host))
                && entry
                    .model_prefix
                    .is_none_or(|prefix| leaf.starts_with(prefix))
        })
        .max_by_key(|entry| {
            usize::from(entry.host.is_some()) * 1000
                + usize::from(entry.model_prefix.is_some()) * 1000
                + entry.host.map(str::len).unwrap_or(0)
                + entry.model_prefix.map(str::len).unwrap_or(0)
        })
        .map(|entry| entry.preset)
}

pub fn describe_provider_presets() -> Vec<Value> {
    provider_presets()
        .into_iter()
        .map(|entry| {
            json!({
                "host": entry.host,
                "modelPrefix": entry.model_prefix,
                "temperature": entry.preset.temperature,
                "topP": entry.preset.top_p,
                "maxTokens": entry.preset.max_tokens,
                "reasoningEffort": entry.preset.reasoning_effort,
                "extraBody": if entry.preset.extra_body.is_empty() {
                    Value::Null
                } else {
                    Value::Object(entry.preset.extra_body)
                },
            })
        })
        .collect()
}
