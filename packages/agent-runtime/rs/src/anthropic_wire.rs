//! Anthropic native-protocol wire helpers (Python `llm.anthropic_native`).

use serde_json::{json, Map, Value};

use crate::provider::{LLMStreamChunk, LLMUsage};
use crate::types::{ContentPart, LLMMessage, ToolCall};

pub struct AnthropicProvider {
    pub name: String,
    pub model: String,
    pub base_url: String,
    pub api_key: Option<String>,
    pub default_temperature: Option<f64>,
    pub default_max_tokens: i64,
}

impl AnthropicProvider {
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
            default_max_tokens: 1024,
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
        let (system, messages) = split_system_and_messages(messages);
        let mut body = Map::from_iter([
            ("model".into(), json!(self.model)),
            ("messages".into(), Value::Array(messages)),
            (
                "max_tokens".into(),
                json!(max_tokens.unwrap_or(self.default_max_tokens)),
            ),
            ("stream".into(), json!(true)),
        ]);
        if let Some(system) = system {
            body.insert("system".into(), json!(system));
        }
        if let Some(temperature) = temperature.or(self.default_temperature) {
            body.insert("temperature".into(), json!(temperature));
        }
        if let Some(tools) = tools {
            let tools: Vec<Value> = tools.iter().map(openai_tool_to_anthropic).collect();
            if !tools.is_empty() {
                body.insert("tools".into(), Value::Array(tools));
            }
        }
        for (key, value) in extra {
            body.insert(key.clone(), value.clone());
        }
        Value::Object(body)
    }

    pub fn request_headers(&self) -> Vec<(String, String)> {
        let mut headers = vec![
            ("Content-Type".into(), "application/json".into()),
            ("anthropic-version".into(), "2023-06-01".into()),
        ];
        if let Some(key) = self.api_key.as_deref().filter(|key| !key.is_empty()) {
            headers.push(("x-api-key".into(), key.to_string()));
        }
        headers
    }
}

fn encode_content_blocks(message: &LLMMessage) -> Value {
    if message
        .content
        .iter()
        .all(|part| matches!(part, ContentPart::Text(_)))
    {
        return Value::String(message.content_text());
    }
    let mut blocks = Vec::new();
    for part in &message.content {
        match part {
            ContentPart::Text(text) => blocks.push(json!({"type": "text", "text": text})),
            ContentPart::Image { source, media_type } => {
                let source = if source.starts_with("http://") || source.starts_with("https://") {
                    json!({"type": "url", "url": source})
                } else {
                    json!({
                        "type": "base64",
                        "media_type": media_type,
                        "data": source,
                    })
                };
                blocks.push(json!({"type": "image", "source": source}));
            }
        }
    }
    Value::Array(blocks)
}

pub fn split_system_and_messages(messages: &[LLMMessage]) -> (Option<String>, Vec<Value>) {
    let mut system_chunks = Vec::new();
    let mut out = Vec::new();
    for message in messages {
        if message.role == "system" {
            let text = message.content_text();
            if !text.is_empty() {
                system_chunks.push(text);
            }
            continue;
        }
        if message.role == "tool" {
            out.push(json!({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id.clone().unwrap_or_default(),
                    "content": message.content_text(),
                }]
            }));
            continue;
        }
        if !message.tool_calls.is_empty() {
            let mut blocks = Vec::new();
            let text = message.content_text();
            if !text.is_empty() {
                blocks.push(json!({"type": "text", "text": text}));
            }
            for tc in &message.tool_calls {
                let input = if tc.arguments.is_object() {
                    tc.arguments.clone()
                } else {
                    json!({})
                };
                blocks.push(json!({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": input,
                }));
            }
            out.push(json!({"role": "assistant", "content": blocks}));
            continue;
        }
        out.push(json!({
            "role": message.role,
            "content": encode_content_blocks(message),
        }));
    }
    let system = if system_chunks.is_empty() {
        None
    } else {
        Some(system_chunks.join("\n\n"))
    };
    (system, out)
}

pub fn openai_tool_to_anthropic(tool: &Value) -> Value {
    if tool.get("name").is_some() && tool.get("input_schema").is_some() {
        return tool.clone();
    }
    let function = tool.get("function").cloned().unwrap_or(json!({}));
    let mut out = Map::new();
    let name = function
        .get("name")
        .or_else(|| tool.get("name"))
        .cloned()
        .unwrap_or(Value::Null);
    out.insert("name".into(), name);
    let description = function
        .get("description")
        .or_else(|| tool.get("description"))
        .cloned()
        .unwrap_or(Value::String(String::new()));
    out.insert("description".into(), description);
    let schema = function
        .get("parameters")
        .cloned()
        .unwrap_or_else(|| json!({"type": "object", "properties": {}}));
    out.insert("input_schema".into(), schema);
    if let Some(cache_control) = tool.get("cache_control") {
        out.insert("cache_control".into(), cache_control.clone());
    }
    Value::Object(out)
}

pub fn parse_anthropic_usage(usage: &Value) -> LLMUsage {
    let prompt = usage
        .get("input_tokens")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let completion = usage
        .get("output_tokens")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    LLMUsage {
        prompt_tokens: prompt,
        completion_tokens: completion,
        total_tokens: prompt + completion,
        cached_prompt_tokens: usage
            .get("cache_read_input_tokens")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        cache_creation_tokens: usage
            .get("cache_creation_input_tokens")
            .and_then(Value::as_i64)
            .unwrap_or(0),
    }
}

pub fn parse_anthropic_event(event: &Value) -> Option<LLMStreamChunk> {
    match event.get("type").and_then(Value::as_str).unwrap_or("") {
        "content_block_start"
            if event.pointer("/content_block/type").and_then(Value::as_str) == Some("tool_use") =>
        {
            let block = event.get("content_block").unwrap_or(&Value::Null);
            Some(LLMStreamChunk {
                tool_call_delta: Some(ToolCall {
                    id: block
                        .get("id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string(),
                    name: block
                        .get("name")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string(),
                    arguments: block.get("input").cloned().unwrap_or_else(|| json!({})),
                }),
                ..LLMStreamChunk::default()
            })
        }
        "content_block_delta" => match event.pointer("/delta/type").and_then(Value::as_str) {
            Some("text_delta") => Some(LLMStreamChunk {
                content_delta: event
                    .pointer("/delta/text")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                ..LLMStreamChunk::default()
            }),
            Some("thinking_delta") => Some(LLMStreamChunk {
                reasoning_delta: event
                    .pointer("/delta/thinking")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                ..LLMStreamChunk::default()
            }),
            Some("input_json_delta") => Some(LLMStreamChunk {
                tool_call_delta: Some(ToolCall {
                    id: String::new(),
                    name: String::new(),
                    arguments: event
                        .pointer("/delta/partial_json")
                        .and_then(Value::as_str)
                        .and_then(|text| serde_json::from_str(text).ok())
                        .unwrap_or_else(|| json!({})),
                }),
                ..LLMStreamChunk::default()
            }),
            _ => None,
        },
        "message_delta" => Some(LLMStreamChunk {
            finish_reason: event
                .pointer("/delta/stop_reason")
                .and_then(Value::as_str)
                .map(|reason| {
                    if reason == "max_tokens" {
                        "length".into()
                    } else {
                        "stop".into()
                    }
                }),
            usage: event.get("usage").map(parse_anthropic_usage),
            ..LLMStreamChunk::default()
        }),
        "message_start" => event
            .pointer("/message/usage")
            .map(parse_anthropic_usage)
            .map(|usage| LLMStreamChunk {
                usage: Some(usage),
                ..LLMStreamChunk::default()
            }),
        _ => None,
    }
}
