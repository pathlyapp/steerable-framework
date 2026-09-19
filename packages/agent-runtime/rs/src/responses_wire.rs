//! OpenAI Responses API item encoding and typed-event decoding.

use std::collections::BTreeMap;

use serde_json::{json, Map, Value};

use crate::presets::preset_for;
use crate::provider::{LLMStreamChunk, LLMUsage};
use crate::types::{ContentPart, LLMMessage, ToolCall};

pub struct OpenAIResponsesProvider {
    pub name: String,
    pub model: String,
    pub base_url: String,
    pub api_key: Option<String>,
    pub default_temperature: Option<f64>,
    pub default_max_tokens: Option<i64>,
    pub reasoning_effort: Option<String>,
}

impl OpenAIResponsesProvider {
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
        let (instructions, input) = encode_responses_input(messages);
        let preset = preset_for(&self.base_url, &self.model);
        let mut body = Map::from_iter([
            ("model".into(), json!(self.model)),
            ("input".into(), Value::Array(input)),
            ("stream".into(), json!(stream)),
            ("store".into(), json!(false)),
        ]);
        if !instructions.is_empty() {
            body.insert("instructions".into(), json!(instructions));
        }
        if let Some(temperature) = temperature
            .or(self.default_temperature)
            .or_else(|| preset.as_ref().and_then(|preset| preset.temperature))
        {
            body.insert("temperature".into(), json!(temperature));
        }
        if let Some(max_tokens) = max_tokens
            .or(self.default_max_tokens)
            .or_else(|| preset.as_ref().and_then(|preset| preset.max_tokens))
        {
            body.insert("max_output_tokens".into(), json!(max_tokens));
        }
        if let Some(tools) = tools {
            let tools: Vec<Value> = tools.iter().map(responses_tool).collect();
            if !tools.is_empty() {
                body.insert("tools".into(), Value::Array(tools));
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
        let effort = self
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
        if let Some(effort) = effort {
            if !body.contains_key("reasoning") {
                body.insert("reasoning".into(), json!({"effort": effort}));
            }
        }
        body.entry("include")
            .or_insert_with(|| json!(["reasoning.encrypted_content"]));
        Value::Object(body)
    }

    pub fn request_headers(&self) -> Vec<(String, String)> {
        let mut headers = vec![("Content-Type".into(), "application/json".into())];
        if let Some(key) = self.api_key.as_deref().filter(|key| !key.is_empty()) {
            headers.push(("Authorization".into(), format!("Bearer {key}")));
        }
        headers
    }
}

pub fn responses_tool(tool: &Value) -> Value {
    let Some(function) = tool.get("function").and_then(Value::as_object) else {
        return tool.clone();
    };
    let mut out = Map::from_iter([
        ("type".into(), json!("function")),
        (
            "name".into(),
            function.get("name").cloned().unwrap_or(json!("")),
        ),
    ]);
    for key in ["description", "parameters"] {
        if let Some(value) = function.get(key) {
            if !value.is_null() && (key != "description" || value.as_str() != Some("")) {
                out.insert(key.into(), value.clone());
            }
        }
    }
    Value::Object(out)
}

pub fn encode_responses_input(messages: &[LLMMessage]) -> (String, Vec<Value>) {
    let mut system = Vec::new();
    let mut items = Vec::new();
    for message in messages {
        if message.role == "system" {
            let text = message.content_text();
            if !text.is_empty() {
                system.push(text);
            }
            continue;
        }
        if message.role == "tool" {
            items.push(json!({
                "type": "function_call_output",
                "call_id": message.tool_call_id.as_deref().unwrap_or(""),
                "output": message.content_text(),
            }));
            continue;
        }
        if message.role == "assistant" {
            if let Some(Value::Array(details)) = &message.reasoning_details {
                items.extend(
                    details
                        .iter()
                        .filter(|item| {
                            item.get("type").and_then(Value::as_str) == Some("reasoning")
                        })
                        .cloned(),
                );
            }
            let content = responses_content(message, true);
            if !content.is_empty() {
                items.push(json!({"type": "message", "role": "assistant", "content": content}));
            }
            items.extend(message.tool_calls.iter().map(|call| {
                json!({
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": call.arguments.to_string(),
                })
            }));
            continue;
        }
        items.push(json!({
            "type": "message",
            "role": message.role,
            "content": responses_content(message, false),
        }));
    }
    (system.join("\n\n"), items)
}

fn responses_content(message: &LLMMessage, output: bool) -> Vec<Value> {
    message
        .content
        .iter()
        .map(|part| match part {
            ContentPart::Text(text) => json!({
                "type": if output { "output_text" } else { "input_text" },
                "text": text,
            }),
            ContentPart::Image { source, media_type } => {
                let url = if source.starts_with("http://") || source.starts_with("https://") {
                    source.clone()
                } else {
                    format!("data:{media_type};base64,{source}")
                };
                json!({"type": "input_image", "image_url": url})
            }
        })
        .collect()
}

#[derive(Default)]
pub struct ResponsesToolCallAssembler {
    slots: BTreeMap<u64, (String, String, String)>,
}

impl ResponsesToolCallAssembler {
    pub fn observe(&mut self, event: &Value) {
        let kind = event.get("type").and_then(Value::as_str).unwrap_or("");
        let index = event
            .get("output_index")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let slot = self.slots.entry(index).or_default();
        match kind {
            "response.output_item.added" => {
                let item = event.get("item").unwrap_or(&Value::Null);
                if item.get("type").and_then(Value::as_str) != Some("function_call") {
                    return;
                }
                slot.0 = item
                    .get("call_id")
                    .or_else(|| item.get("id"))
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                slot.1 = item
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                if let Some(arguments) = item.get("arguments").and_then(Value::as_str) {
                    slot.2 = arguments.to_string();
                }
            }
            "response.function_call_arguments.delta" => {
                slot.2
                    .push_str(event.get("delta").and_then(Value::as_str).unwrap_or(""));
            }
            "response.function_call_arguments.done" => {
                if let Some(arguments) = event.get("arguments").and_then(Value::as_str) {
                    slot.2 = arguments.to_string();
                }
            }
            _ => {}
        }
    }

    pub fn flush(&mut self) -> Vec<ToolCall> {
        let slots = std::mem::take(&mut self.slots);
        slots
            .into_values()
            .filter(|(_, name, _)| !name.is_empty())
            .map(|(id, name, arguments)| ToolCall {
                id,
                name,
                arguments: serde_json::from_str(&arguments)
                    .ok()
                    .filter(Value::is_object)
                    .unwrap_or_else(|| json!({})),
            })
            .collect()
    }
}

pub fn parse_responses_usage(usage: &Value) -> LLMUsage {
    LLMUsage {
        prompt_tokens: usage
            .get("input_tokens")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        completion_tokens: usage
            .get("output_tokens")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        total_tokens: usage
            .get("total_tokens")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        cached_prompt_tokens: usage
            .pointer("/input_tokens_details/cached_tokens")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        cache_creation_tokens: 0,
    }
}

pub fn parse_responses_event(event: &Value) -> Option<LLMStreamChunk> {
    match event.get("type").and_then(Value::as_str).unwrap_or("") {
        "response.output_text.delta" => Some(LLMStreamChunk {
            content_delta: event
                .get("delta")
                .and_then(Value::as_str)
                .map(str::to_string),
            ..LLMStreamChunk::default()
        }),
        "response.reasoning_text.delta" | "response.reasoning_summary_text.delta" => {
            Some(LLMStreamChunk {
                reasoning_delta: event
                    .get("delta")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                ..LLMStreamChunk::default()
            })
        }
        "response.output_item.done"
            if event.pointer("/item/type").and_then(Value::as_str) == Some("reasoning") =>
        {
            Some(LLMStreamChunk {
                reasoning_details: Some(json!([event.get("item").cloned().unwrap_or(Value::Null)])),
                ..LLMStreamChunk::default()
            })
        }
        "response.completed" => {
            let response = event.get("response").unwrap_or(&Value::Null);
            let finish = if response.get("status").and_then(Value::as_str) == Some("incomplete")
                && response
                    .pointer("/incomplete_details/reason")
                    .and_then(Value::as_str)
                    == Some("max_output_tokens")
            {
                "length"
            } else {
                "stop"
            };
            Some(LLMStreamChunk {
                finish_reason: Some(finish.into()),
                usage: response.get("usage").map(parse_responses_usage),
                ..LLMStreamChunk::default()
            })
        }
        _ => None,
    }
}
