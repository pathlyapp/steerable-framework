//! OpenAI chat-completions wire helpers (Python `llm.openai_compat`).

use std::collections::BTreeMap;

use serde_json::{json, Map, Value};

use crate::compat::OpenAICompatFlags;
use crate::json_py::dumps;
use crate::presets::preset_for;
use crate::provider::{LLMStreamChunk, LLMUsage};
use crate::types::{ContentPart, LLMMessage, ToolCall};

const HARMONY_NAME_PREFIXES: &[&str] = &["to=", "functions."];
const DEFAULT_CONNECT_SEC: f64 = 30.0;
const DEFAULT_STREAM_READ_SEC: f64 = 300.0;

pub fn sanitize_tool_name(name: &str) -> String {
    if !name.contains("<|") && !HARMONY_NAME_PREFIXES.iter().any(|p| name.starts_with(p)) {
        return name.to_string();
    }
    let mut cleaned = name.split("<|").next().unwrap_or(name).to_string();
    for prefix in HARMONY_NAME_PREFIXES {
        if let Some(rest) = cleaned.strip_prefix(prefix) {
            cleaned = rest.to_string();
        }
    }
    cleaned.trim().to_string()
}

fn encode_content(message: &LLMMessage) -> Value {
    if message
        .content
        .iter()
        .all(|part| matches!(part, ContentPart::Text(_)))
    {
        return Value::String(message.content_text());
    }
    let mut out = Vec::new();
    for part in &message.content {
        match part {
            ContentPart::Text(text) => {
                out.push(json!({"type": "text", "text": text}));
            }
            ContentPart::Image { source, media_type } => {
                let url = if source.starts_with("http://")
                    || source.starts_with("https://")
                    || source.starts_with("data:")
                {
                    source.clone()
                } else {
                    format!("data:{media_type};base64,{source}")
                };
                out.push(json!({"type": "image_url", "image_url": {"url": url}}));
            }
        }
    }
    Value::Array(out)
}

pub fn encode_message(message: &LLMMessage, compat: &OpenAICompatFlags) -> Value {
    let mut out = Map::new();
    out.insert("role".into(), Value::String(message.role.clone()));
    out.insert("content".into(), encode_content(message));
    if let Some(name) = &message.name {
        out.insert("name".into(), Value::String(name.clone()));
    }
    if let Some(tool_call_id) = &message.tool_call_id {
        out.insert("tool_call_id".into(), Value::String(tool_call_id.clone()));
    }
    if !message.tool_calls.is_empty() {
        let calls: Vec<Value> = message
            .tool_calls
            .iter()
            .map(|tc| {
                json!({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": dumps(&tc.arguments),
                    }
                })
            })
            .collect();
        out.insert("tool_calls".into(), Value::Array(calls));
    }
    if let Some(details) = &message.reasoning_details {
        out.insert("reasoning_details".into(), details.clone());
    } else if let Some(reasoning) = &message.reasoning {
        out.insert(
            compat.reasoning_echo_field.into(),
            Value::String(reasoning.clone()),
        );
    } else if !message.tool_calls.is_empty() && compat.echo_empty_reasoning_for_tool_calls {
        out.insert(
            compat.reasoning_echo_field.into(),
            Value::String(String::new()),
        );
    }
    Value::Object(out)
}

pub fn decode_tool_calls(value: &Value) -> Option<Vec<ToolCall>> {
    let items = value.as_array()?;
    if items.is_empty() {
        return None;
    }
    let mut out = Vec::new();
    for item in items {
        let function = item.get("function").cloned().unwrap_or(json!({}));
        let arguments = match function.get("arguments") {
            Some(Value::String(s)) if !s.is_empty() => {
                serde_json::from_str(s).unwrap_or_else(|_| json!({}))
            }
            _ => json!({}),
        };
        out.push(ToolCall {
            id: item
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            name: sanitize_tool_name(function.get("name").and_then(Value::as_str).unwrap_or("")),
            arguments,
        });
    }
    if out.is_empty() {
        None
    } else {
        Some(out)
    }
}

fn resolve_path<'a>(obj: &'a Value, path: &str) -> Option<&'a Value> {
    let mut cur = obj;
    for part in path.split('.') {
        cur = cur.get(part)?;
    }
    Some(cur)
}

pub fn parse_usage(usage: &Value, compat: &OpenAICompatFlags) -> LLMUsage {
    let mut cached = 0i64;
    for path in compat.cached_tokens_fields {
        if let Some(value) = resolve_path(usage, path) {
            if let Some(n) = json_i64(value) {
                if n != 0 {
                    cached = n;
                    break;
                }
            }
        }
    }
    LLMUsage {
        prompt_tokens: usage.get("prompt_tokens").and_then(json_i64).unwrap_or(0),
        completion_tokens: usage
            .get("completion_tokens")
            .and_then(json_i64)
            .unwrap_or(0),
        total_tokens: usage.get("total_tokens").and_then(json_i64).unwrap_or(0),
        cached_prompt_tokens: cached,
        cache_creation_tokens: 0,
    }
}

fn json_i64(value: &Value) -> Option<i64> {
    match value {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)),
        Value::String(s) => s.parse().ok(),
        _ => None,
    }
}

fn reasoning_text(delta: &Value, fields: &[&str]) -> Option<String> {
    let mut raw: Option<&Value> = None;
    for field in fields {
        if let Some(value) = delta.get(*field) {
            if !value.is_null() {
                raw = Some(value);
                break;
            }
        }
    }
    match raw {
        Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
        Some(Value::Array(items)) => {
            let mut parts = Vec::new();
            for item in items {
                if let Some(s) = item.as_str() {
                    if !s.is_empty() {
                        parts.push(s.to_string());
                    }
                } else if let Some(obj) = item.as_object() {
                    if let Some(text) = obj
                        .get("text")
                        .or_else(|| obj.get("content"))
                        .and_then(Value::as_str)
                    {
                        parts.push(text.to_string());
                    }
                }
            }
            if parts.is_empty() {
                None
            } else {
                Some(parts.concat())
            }
        }
        _ => None,
    }
}

fn reasoning_details_list(value: Option<&Value>) -> Option<Value> {
    match value {
        Some(Value::Array(items)) if !items.is_empty() => Some(Value::Array(items.clone())),
        _ => None,
    }
}

pub fn parse_stream_chunk(chunk: &Value, compat: &OpenAICompatFlags) -> Option<LLMStreamChunk> {
    let choices = chunk.get("choices").and_then(Value::as_array);
    if choices.map(|c| c.is_empty()).unwrap_or(true) {
        if let Some(usage) = chunk.get("usage") {
            return Some(LLMStreamChunk {
                usage: Some(parse_usage(usage, compat)),
                ..LLMStreamChunk::default()
            });
        }
        return None;
    }
    let choice = &choices.unwrap()[0];
    let delta = choice.get("delta").cloned().unwrap_or(json!({}));
    let finish_reason = choice
        .get("finish_reason")
        .and_then(Value::as_str)
        .map(str::to_string);
    let content = delta
        .get("content")
        .and_then(Value::as_str)
        .map(str::to_string);
    let reasoning = reasoning_text(&delta, compat.reasoning_delta_fields);
    let mut tool_call_delta = None;
    if let Some(raw_tool_calls) = delta.get("tool_calls").and_then(Value::as_array) {
        if let Some(first) = raw_tool_calls.first() {
            let function = first.get("function").cloned().unwrap_or(json!({}));
            let raw_args = function.get("arguments").cloned().unwrap_or(json!({}));
            let arguments = match &raw_args {
                Value::Object(_) => raw_args,
                Value::String(s) if !s.is_empty() => {
                    serde_json::from_str(s).unwrap_or_else(|_| json!({}))
                }
                _ => json!({}),
            };
            let arguments = if arguments.is_object() {
                arguments
            } else {
                json!({})
            };
            tool_call_delta = Some(ToolCall {
                id: first
                    .get("id")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                name: sanitize_tool_name(
                    function.get("name").and_then(Value::as_str).unwrap_or(""),
                ),
                arguments,
            });
        }
    }
    let usage = chunk.get("usage").map(|u| parse_usage(u, compat));
    Some(LLMStreamChunk {
        content_delta: content,
        reasoning_delta: reasoning,
        reasoning_details: reasoning_details_list(delta.get("reasoning_details")),
        tool_call_delta,
        finish_reason,
        usage,
    })
}

#[derive(Default)]
pub struct OpenAIToolCallAssembler {
    buf: BTreeMap<i64, (String, String, String)>,
}

impl OpenAIToolCallAssembler {
    pub fn observe(&mut self, chunk: &Value) {
        let Some(choices) = chunk.get("choices").and_then(Value::as_array) else {
            return;
        };
        if choices.is_empty() {
            return;
        }
        let delta = choices[0].get("delta").cloned().unwrap_or(json!({}));
        let Some(items) = delta.get("tool_calls").and_then(Value::as_array) else {
            return;
        };
        for item in items {
            let idx = item.get("index").and_then(json_i64).unwrap_or(0);
            let slot = self
                .buf
                .entry(idx)
                .or_insert_with(|| (String::new(), String::new(), String::new()));
            if let Some(id) = item.get("id").and_then(Value::as_str) {
                if !id.is_empty() {
                    slot.0 = id.to_string();
                }
            }
            let function = item.get("function").cloned().unwrap_or(json!({}));
            if let Some(name) = function.get("name").and_then(Value::as_str) {
                slot.1.push_str(name);
            }
            match function.get("arguments") {
                Some(Value::String(raw)) if !raw.is_empty() => slot.2.push_str(raw),
                Some(Value::Object(obj)) => slot.2 = dumps(&Value::Object(obj.clone())),
                _ => {}
            }
        }
    }

    pub fn flush(&mut self) -> Vec<ToolCall> {
        let mut calls = Vec::new();
        for (_idx, (id, name, arguments)) in std::mem::take(&mut self.buf) {
            let name = sanitize_tool_name(&name);
            if name.is_empty() {
                continue;
            }
            let raw = if arguments.is_empty() {
                "{}"
            } else {
                arguments.as_str()
            };
            let parsed: Value = serde_json::from_str(raw).unwrap_or_else(|_| json!({}));
            let arguments = if parsed.is_object() {
                parsed
            } else {
                json!({})
            };
            calls.push(ToolCall {
                id,
                name,
                arguments,
            });
        }
        calls
    }
}

pub fn glm_z_ai_host(base_url: &str) -> bool {
    let host = base_url.to_ascii_lowercase();
    host.contains("z.ai") || host.contains("bigmodel.cn")
}

pub fn openrouter_host(base_url: &str) -> bool {
    base_url.to_ascii_lowercase().contains("openrouter.ai")
}

pub fn z_ai_tool_choice_auto_only(model: &str, base_url: &str) -> bool {
    if glm_z_ai_host(base_url) {
        return true;
    }
    let lowered = model.to_ascii_lowercase();
    openrouter_host(base_url) && (lowered.contains("z-ai") || lowered.contains("glm"))
}

pub fn thinking_rejects_forced_tool_choice(model: &str, base_url: &str) -> bool {
    if z_ai_tool_choice_auto_only(model, base_url) {
        return true;
    }
    let lowered = model.to_ascii_lowercase();
    lowered.contains("qwen") || lowered.contains("deepseek")
}

fn env_flag(name: &str) -> Option<bool> {
    let raw = std::env::var(name).ok()?.trim().to_ascii_lowercase();
    match raw.as_str() {
        "1" | "true" | "yes" | "on" => Some(true),
        "0" | "false" | "no" | "off" => Some(false),
        _ => None,
    }
}

pub fn openrouter_provider_prefs() -> Option<Value> {
    let raw = std::env::var("STEERABLE_OPENROUTER_PROVIDER").ok()?;
    let raw = raw.trim();
    if raw.is_empty() {
        return None;
    }
    let order: Vec<Value> = raw
        .split(',')
        .map(str::trim)
        .filter(|part| !part.is_empty())
        .map(|part| Value::String(part.to_string()))
        .collect();
    if order.is_empty() {
        return None;
    }
    let mut prefs = Map::new();
    prefs.insert("order".into(), Value::Array(order.clone()));
    prefs.insert("only".into(), Value::Array(order));
    if let Some(fallbacks) = env_flag("STEERABLE_OPENROUTER_ALLOW_FALLBACKS") {
        prefs.insert("allow_fallbacks".into(), Value::Bool(fallbacks));
    }
    if let Some(require) = env_flag("STEERABLE_OPENROUTER_REQUIRE_PARAMETERS") {
        prefs.insert("require_parameters".into(), Value::Bool(require));
    }
    Some(Value::Object(prefs))
}

pub fn timeout_sec(name: &str, default: f64) -> f64 {
    let Ok(raw) = std::env::var(name) else {
        return default;
    };
    let raw = raw.trim();
    if raw.is_empty() {
        return default;
    }
    match raw.parse::<f64>() {
        Ok(value) if value > 0.0 => value,
        _ => default,
    }
}

pub fn stream_timeout() -> (f64, f64) {
    (
        timeout_sec("STEERABLE_LLM_CONNECT_TIMEOUT_SEC", DEFAULT_CONNECT_SEC),
        timeout_sec(
            "STEERABLE_LLM_STREAM_READ_TIMEOUT_SEC",
            DEFAULT_STREAM_READ_SEC,
        ),
    )
}

pub struct OpenAICompatProvider {
    pub name: String,
    pub model: String,
    pub base_url: String,
    pub api_key: Option<String>,
    pub default_temperature: Option<f64>,
    pub default_max_tokens: Option<i64>,
    pub reasoning_effort: Option<String>,
    pub compat: OpenAICompatFlags,
}

impl OpenAICompatProvider {
    pub fn new(
        name: impl Into<String>,
        model: impl Into<String>,
        base_url: impl Into<String>,
    ) -> Self {
        Self {
            name: name.into(),
            model: model.into(),
            base_url: base_url.into(),
            api_key: None,
            default_temperature: None,
            default_max_tokens: None,
            reasoning_effort: None,
            compat: OpenAICompatFlags::default(),
        }
    }

    pub fn build_body(
        &self,
        messages: &[LLMMessage],
        tools: Option<&[Value]>,
        temperature: Option<f64>,
        max_tokens: Option<i64>,
        stream: bool,
        extra: &Map<String, Value>,
    ) -> Value {
        let preset = preset_for(&self.base_url, &self.model);
        let mut body = Map::new();
        body.insert("model".into(), Value::String(self.model.clone()));
        body.insert(
            "messages".into(),
            Value::Array(
                messages
                    .iter()
                    .map(|m| encode_message(m, &self.compat))
                    .collect(),
            ),
        );
        body.insert("stream".into(), Value::Bool(stream));
        if stream && self.compat.supports_usage_in_streaming {
            body.insert("stream_options".into(), json!({"include_usage": true}));
        }
        let eff_temperature = temperature
            .or(self.default_temperature)
            .or_else(|| preset.as_ref().and_then(|preset| preset.temperature));
        if let Some(temp) = eff_temperature {
            if self.compat.supports_temperature {
                body.insert("temperature".into(), json!(temp));
            }
        }
        if let Some(max_tokens) = max_tokens
            .or(self.default_max_tokens)
            .or_else(|| preset.as_ref().and_then(|preset| preset.max_tokens))
        {
            body.insert(self.compat.max_tokens_field.to_string(), json!(max_tokens));
        }
        if let Some(tools) = tools {
            if !tools.is_empty() {
                body.insert("tools".into(), Value::Array(tools.to_vec()));
            }
        }
        for (key, value) in extra {
            body.insert(key.clone(), value.clone());
        }
        if let Some(preset) = &preset {
            if let Some(top_p) = preset.top_p {
                body.entry("top_p").or_insert_with(|| json!(top_p));
            }
            for (key, value) in &preset.extra_body {
                body.entry(key).or_insert_with(|| value.clone());
            }
        }
        if body.get("tool_choice").and_then(Value::as_str) == Some("required")
            && (!self.compat.supports_forced_tool_choice
                || thinking_rejects_forced_tool_choice(&self.model, &self.base_url))
        {
            body.insert("tool_choice".into(), Value::String("auto".into()));
        }
        let reasoning_effort = self
            .reasoning_effort
            .clone()
            .or_else(|| {
                std::env::var("STEERABLE_REASONING_EFFORT")
                    .ok()
                    .filter(|value| !value.is_empty())
            })
            .or_else(|| {
                preset
                    .as_ref()
                    .and_then(|preset| preset.reasoning_effort.clone())
            });
        if self.compat.supports_reasoning_effort {
            if let Some(effort) = reasoning_effort {
                body.entry("reasoning_effort")
                    .or_insert_with(|| json!(effort));
                if openrouter_host(&self.base_url) {
                    body.entry("reasoning")
                        .or_insert_with(|| json!({"effort": effort, "exclude": false}));
                }
            }
        }
        if openrouter_host(&self.base_url) && !body.contains_key("provider") {
            if let Some(prefs) = openrouter_provider_prefs() {
                body.insert("provider".into(), prefs);
            }
        }
        if glm_z_ai_host(&self.base_url) && !body.contains_key("thinking") {
            body.insert("thinking".into(), json!({"type": "enabled"}));
            if stream {
                body.insert("tool_stream".into(), Value::Bool(true));
            }
        }
        Value::Object(body)
    }

    pub fn request_headers(&self) -> Vec<(String, String)> {
        let mut headers = vec![("Content-Type".into(), "application/json".into())];
        if let Some(api_key) = &self.api_key {
            if !api_key.is_empty() {
                headers.push(("Authorization".into(), format!("Bearer {api_key}")));
            }
        }
        if let Ok(referer) = std::env::var("STEERABLE_HTTP_REFERER") {
            let referer = referer.trim();
            if !referer.is_empty() {
                headers.push(("HTTP-Referer".into(), referer.to_string()));
            }
        }
        if let Ok(title) = std::env::var("STEERABLE_HTTP_TITLE") {
            let title = title.trim();
            if !title.is_empty() {
                headers.push(("X-Title".into(), title.to_string()));
            }
        }
        headers
    }
}

pub fn consume_sse_lines<I, S>(lines: I, compat: &OpenAICompatFlags) -> Vec<LLMStreamChunk>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let mut assembler = OpenAIToolCallAssembler::default();
    let mut out = Vec::new();
    for line in lines {
        let mut line = line.as_ref().trim_end_matches('\r').to_string();
        if line.is_empty() || line.starts_with(':') {
            continue;
        }
        if let Some(rest) = line.strip_prefix("data:") {
            line = rest.trim().to_string();
        }
        if line == "[DONE]" {
            for call in assembler.flush() {
                out.push(LLMStreamChunk {
                    tool_call_delta: Some(call),
                    ..LLMStreamChunk::default()
                });
            }
            return out;
        }
        let Ok(chunk) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        assembler.observe(&chunk);
        let Some(mut parsed) = parse_stream_chunk(&chunk, compat) else {
            continue;
        };
        parsed.tool_call_delta = None;
        let has_content = parsed
            .content_delta
            .as_ref()
            .is_some_and(|text| !text.is_empty());
        if has_content
            || parsed.reasoning_delta.is_some()
            || parsed.reasoning_details.is_some()
            || parsed.finish_reason.is_some()
            || parsed.usage.is_some()
        {
            out.push(parsed);
        }
    }
    for call in assembler.flush() {
        out.push(LLMStreamChunk {
            tool_call_delta: Some(call),
            ..LLMStreamChunk::default()
        });
    }
    out
}
