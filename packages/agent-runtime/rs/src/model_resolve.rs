//! Resolution over the generated models.dev catalog.

use std::sync::LazyLock;

use serde_json::{json, Map, Value};

const MIN_PREFIX_CHARS: usize = 6;

static CATALOG: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("model_catalog.json")).expect("generated model catalog JSON")
});

#[derive(Clone, Debug, PartialEq)]
pub struct CatalogHit {
    pub key: String,
    pub context_window: i64,
    pub input_modalities: Vec<String>,
    pub tool_format: String,
    pub reasoning_levels: Vec<String>,
    pub source: String,
}

fn models() -> &'static Map<String, Value> {
    CATALOG["models"]
        .as_object()
        .expect("model catalog models object")
}

fn hit(key: &str, source: &str) -> Option<CatalogHit> {
    let row = models().get(key)?.as_array()?;
    Some(CatalogHit {
        key: key.to_string(),
        context_window: row.first()?.as_i64()?,
        input_modalities: row
            .get(1)?
            .as_array()?
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_string)
            .collect(),
        tool_format: row.get(2)?.as_str()?.to_string(),
        reasoning_levels: row
            .get(3)?
            .as_array()?
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_string)
            .collect(),
        source: source.to_string(),
    })
}

pub fn resolve_in_catalog(provider: Option<&str>, model: &str) -> Option<CatalogHit> {
    if model.is_empty() {
        return None;
    }
    if let Some(provider) = provider {
        let exact = format!("{provider}/{model}");
        if models().contains_key(&exact) {
            return hit(&exact, "exact");
        }
    }
    if model.contains('/') && models().contains_key(model) {
        return hit(model, "exact");
    }
    let provider = provider?;
    let prefix = format!("{provider}/");
    if let Some(key) = models().keys().find(|key| {
        key.starts_with(&prefix)
            && key[prefix.len()..]
                .rsplit('/')
                .next()
                .is_some_and(|leaf| leaf == model)
    }) {
        return hit(key, "scoped");
    }
    models()
        .keys()
        .filter_map(|key| {
            let (key_provider, model_id) = key.split_once('/')?;
            (key_provider == provider
                && model_id.len() >= MIN_PREFIX_CHARS
                && model.starts_with(model_id))
            .then_some(key)
        })
        .max_by_key(|key| key.split_once('/').map(|(_, id)| id.len()).unwrap_or(0))
        .and_then(|key| hit(key, "prefix"))
}

pub fn resolve_leaf_cross_provider(model: &str) -> Option<CatalogHit> {
    let leaf = model
        .rsplit('/')
        .next()?
        .split(':')
        .next()?
        .to_ascii_lowercase();
    let mut matches: Vec<&String> = models()
        .keys()
        .filter(|key| {
            key.rsplit('/')
                .next()
                .is_some_and(|candidate| candidate.eq_ignore_ascii_case(&leaf))
        })
        .collect();
    matches.sort_by_key(|key| {
        let resolved = hit(key, "leaf").expect("catalog row");
        (
            resolved.reasoning_levels.is_empty(),
            resolved.context_window,
            (*key).clone(),
        )
    });
    matches.first().and_then(|key| hit(key, "leaf"))
}

pub fn describe_catalog_providers() -> Vec<Value> {
    let providers = CATALOG["providers"]
        .as_object()
        .expect("model catalog providers object");
    let mut result = Vec::new();
    for (provider, row) in providers {
        let row = row.as_array().expect("provider row");
        let mut provider_models: Vec<(&String, CatalogHit)> = models()
            .keys()
            .filter(|key| key.starts_with(&format!("{provider}/")))
            .filter_map(|key| hit(key, "exact").map(|resolved| (key, resolved)))
            .filter(|(_, resolved)| resolved.tool_format != "none")
            .collect();
        if provider_models.is_empty() {
            continue;
        }
        provider_models.sort_by_key(|(key, _)| (*key).clone());
        let formats: Vec<&str> = provider_models
            .iter()
            .map(|(_, resolved)| resolved.tool_format.as_str())
            .collect();
        let api = row.first().cloned().unwrap_or(Value::Null);
        let default_api = match provider.as_str() {
            "openai" => Some("https://api.openai.com/v1"),
            "anthropic" => Some("https://api.anthropic.com"),
            "google" => Some("https://generativelanguage.googleapis.com"),
            "groq" => Some("https://api.groq.com/openai/v1"),
            "xai" => Some("https://api.x.ai/v1"),
            _ => None,
        };
        let wire = match provider.as_str() {
            "anthropic" | "google-vertex-anthropic" => "anthropic",
            "google" | "google-vertex" => "google",
            "xai" => "openai-responses",
            _ if formats.iter().all(|format| *format == "anthropic") => "anthropic",
            _ => "openai_compat",
        };
        result.push(json!({
            "id": provider,
            "apiBaseUrl": if api.is_null() { default_api.map(Value::from).unwrap_or(Value::Null) } else { api },
            "envVars": row.get(1).cloned().unwrap_or_else(|| json!([])),
            "wireKind": wire,
            "models": provider_models.iter().map(|(key, _)| key.split_once('/').unwrap().1).collect::<Vec<_>>(),
        }));
    }
    result.sort_by_key(|row| row["id"].as_str().unwrap_or("").to_string());
    result
}

pub fn catalog_provider_for_base_url(base_url: &str) -> Option<String> {
    let host = base_url
        .split_once("://")
        .map(|(_, rest)| rest)
        .unwrap_or(base_url)
        .split('/')
        .next()?
        .split(':')
        .next()?
        .to_ascii_lowercase();
    CATALOG["providers"]
        .as_object()?
        .iter()
        .find_map(|(provider, row)| {
            let api = row.as_array()?.first()?.as_str()?;
            let api_host = api
                .split_once("://")
                .map(|(_, rest)| rest)
                .unwrap_or(api)
                .split('/')
                .next()?
                .split(':')
                .next()?;
            api_host
                .eq_ignore_ascii_case(&host)
                .then(|| provider.clone())
        })
}
