//! Google Gemini native content encoding and stream decoding.

use serde_json::{json, Map, Value};

use crate::presets::preset_for;
use crate::provider::{LLMStreamChunk, LLMUsage};
use crate::types::{ContentPart, LLMMessage, ToolCall};

pub struct GoogleGenAIProvider {
    pub name: String,
    pub model: String,
    pub base_url: String,
    pub api_key: Option<String>,
    pub default_temperature: Option<f64>,
    pub default_max_tokens: Option<i64>,
}

impl GoogleGenAIProvider {
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
        }
    }

    pub fn build_body(
        &self,
        messages: &[LLMMessage],
        tools: Option<&[Value]>,
        temperature: Option<f64>,
        max_tokens: Option<i64>,
        extra: &Map<String, Value>,
    ) -> Value {
        let (system, contents) = encode_gemini_contents(messages);
        let preset = preset_for(&self.base_url, &self.model);
        let mut body = Map::from_iter([("contents".into(), Value::Array(contents))]);
        if !system.is_empty() {
            body.insert(
                "systemInstruction".into(),
                json!({"parts": [{"text": system}]}),
            );
        }
        if let Some(tools) = tools {
            let declarations: Vec<Value> = tools.iter().filter_map(gemini_tool).collect();
            if !declarations.is_empty() {
                body.insert(
                    "tools".into(),
                    json!([{"functionDeclarations": declarations}]),
                );
            }
        }
        let mut generation = Map::new();
        if let Some(temperature) = temperature
            .or(self.default_temperature)
            .or_else(|| preset.as_ref().and_then(|preset| preset.temperature))
        {
            generation.insert("temperature".into(), json!(temperature));
        }
        if let Some(max_tokens) = max_tokens
            .or(self.default_max_tokens)
            .or_else(|| preset.as_ref().and_then(|preset| preset.max_tokens))
        {
            generation.insert("maxOutputTokens".into(), json!(max_tokens));
        }
        if let Some(top_p) = preset.as_ref().and_then(|preset| preset.top_p) {
            generation.insert("topP".into(), json!(top_p));
        }
        if let Some(Value::Object(extra_generation)) = extra.get("generationConfig") {
            generation.extend(extra_generation.clone());
        }
        if !generation.is_empty() {
            body.insert("generationConfig".into(), Value::Object(generation));
        }
        for (key, value) in extra {
            if key != "generationConfig" {
                body.insert(key.clone(), value.clone());
            }
        }
        if let Some(preset) = preset {
            for (key, value) in preset.extra_body {
                body.entry(key).or_insert(value);
            }
        }
        Value::Object(body)
    }

    pub fn stream_url(&self) -> String {
        format!(
            "{}/v1beta/models/{}:streamGenerateContent?alt=sse",
            self.base_url.trim_end_matches('/'),
            self.model
        )
    }
}

fn gemini_tool(tool: &Value) -> Option<Value> {
    let function = tool.get("function")?.as_object()?;
    Some(json!({
        "name": function.get("name").cloned().unwrap_or(json!("")),
        "description": function.get("description").cloned().unwrap_or(json!("")),
        "parameters": function.get("parameters").cloned().unwrap_or_else(|| json!({"type": "object"})),
    }))
}

pub fn encode_gemini_contents(messages: &[LLMMessage]) -> (String, Vec<Value>) {
    let mut system = Vec::new();
    let mut contents = Vec::new();
    for message in messages {
        if message.role == "system" {
            let text = message.content_text();
            if !text.is_empty() {
                system.push(text);
            }
            continue;
        }
        if message.role == "tool" {
            contents.push(json!({
                "role": "user",
                "parts": [{
                    "functionResponse": {
                        "name": message.name.as_deref().unwrap_or(""),
                        "response": {"result": message.content_text()},
                    },
                }],
            }));
            continue;
        }
        let mut parts: Vec<Value> = message
            .content
            .iter()
            .map(|part| match part {
                ContentPart::Text(text) => json!({"text": text}),
                ContentPart::Image { source, media_type } => {
                    json!({"inlineData": {"mimeType": media_type, "data": source}})
                }
            })
            .collect();
        parts.extend(
            message
                .tool_calls
                .iter()
                .map(|call| json!({"functionCall": {"name": call.name, "args": call.arguments}})),
        );
        contents.push(json!({
            "role": if message.role == "assistant" { "model" } else { "user" },
            "parts": parts,
        }));
    }
    (system.join("\n\n"), contents)
}

pub fn parse_gemini_usage(value: &Value) -> LLMUsage {
    LLMUsage {
        prompt_tokens: value
            .get("promptTokenCount")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        completion_tokens: value
            .get("candidatesTokenCount")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        total_tokens: value
            .get("totalTokenCount")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        cached_prompt_tokens: value
            .get("cachedContentTokenCount")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        cache_creation_tokens: 0,
    }
}

pub fn parse_gemini_chunk(value: &Value) -> Vec<LLMStreamChunk> {
    let mut out = Vec::new();
    if let Some(parts) = value
        .pointer("/candidates/0/content/parts")
        .and_then(Value::as_array)
    {
        for part in parts {
            if let Some(text) = part.get("text").and_then(Value::as_str) {
                if part
                    .get("thought")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
                {
                    out.push(LLMStreamChunk {
                        reasoning_delta: Some(text.to_string()),
                        ..LLMStreamChunk::default()
                    });
                } else {
                    out.push(LLMStreamChunk {
                        content_delta: Some(text.to_string()),
                        ..LLMStreamChunk::default()
                    });
                }
            }
            if let Some(call) = part.get("functionCall") {
                let name = call.get("name").and_then(Value::as_str).unwrap_or("");
                out.push(LLMStreamChunk {
                    tool_call_delta: Some(ToolCall {
                        id: name.to_string(),
                        name: name.to_string(),
                        arguments: call.get("args").cloned().unwrap_or_else(|| json!({})),
                    }),
                    ..LLMStreamChunk::default()
                });
            }
        }
    }
    if let Some(reason) = value
        .pointer("/candidates/0/finishReason")
        .and_then(Value::as_str)
    {
        let reason = match reason {
            "MAX_TOKENS" => "length",
            "SAFETY" | "RECITATION" => "content_filter",
            _ => "stop",
        };
        out.push(LLMStreamChunk {
            finish_reason: Some(reason.into()),
            ..LLMStreamChunk::default()
        });
    }
    if let Some(usage) = value.get("usageMetadata") {
        out.push(LLMStreamChunk {
            usage: Some(parse_gemini_usage(usage)),
            ..LLMStreamChunk::default()
        });
    }
    out
}
