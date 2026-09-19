//! JSON wire format between the Rust loop and the Python sidecar.

use serde_json::{json, Value};

use crate::budget::BudgetLimit;
use crate::engine::LoopConfig;
use crate::provider::{LLMStreamChunk, LLMUsage};
use crate::types::{ContentPart, LLMMessage, ToolCall, ToolResult};

pub fn messages_to_value(messages: &[LLMMessage]) -> Value {
    Value::Array(messages.iter().map(message_to_value).collect())
}

pub fn message_to_value(message: &LLMMessage) -> Value {
    json!({
        "role": message.role,
        "content": message.content.iter().map(|part| match part {
            ContentPart::Text(text) => json!({ "type": "text", "text": text }),
            ContentPart::Image { source, media_type } => json!({
                "type": "image",
                "source": source,
                "media_type": media_type,
            }),
        }).collect::<Vec<_>>(),
        "name": message.name,
        "tool_call_id": message.tool_call_id,
        "tool_calls": message.tool_calls.iter().map(tool_call_to_value).collect::<Vec<_>>(),
        "reasoning": message.reasoning,
        "reasoning_details": message.reasoning_details,
    })
}

pub fn messages_from_value(value: &Value) -> Result<Vec<LLMMessage>, String> {
    value
        .as_array()
        .ok_or_else(|| "messages must be an array".to_string())?
        .iter()
        .map(message_from_value)
        .collect()
}

pub fn message_from_value(value: &Value) -> Result<LLMMessage, String> {
    let obj = value
        .as_object()
        .ok_or_else(|| "message must be an object".to_string())?;
    let role = obj
        .get("role")
        .and_then(Value::as_str)
        .ok_or_else(|| "message.role required".to_string())?
        .to_string();
    let content = match obj.get("content") {
        Some(Value::String(text)) => vec![ContentPart::Text(text.clone())],
        Some(Value::Array(parts)) => parts
            .iter()
            .map(part_from_value)
            .collect::<Result<_, _>>()?,
        _ => Vec::new(),
    };
    let tool_calls = obj
        .get("tool_calls")
        .and_then(Value::as_array)
        .map(|calls| {
            calls
                .iter()
                .map(tool_call_from_value)
                .collect::<Result<_, _>>()
        })
        .transpose()?
        .unwrap_or_default();
    Ok(LLMMessage {
        role,
        content,
        name: obj.get("name").and_then(Value::as_str).map(str::to_string),
        tool_call_id: obj
            .get("tool_call_id")
            .and_then(Value::as_str)
            .map(str::to_string),
        tool_calls,
        reasoning: obj
            .get("reasoning")
            .and_then(Value::as_str)
            .map(str::to_string),
        reasoning_details: obj
            .get("reasoning_details")
            .cloned()
            .filter(|v| !v.is_null()),
    })
}

fn part_from_value(value: &Value) -> Result<ContentPart, String> {
    let obj = value
        .as_object()
        .ok_or_else(|| "content part must be an object".to_string())?;
    match obj.get("type").and_then(Value::as_str).unwrap_or("text") {
        "image" => Ok(ContentPart::Image {
            source: obj
                .get("source")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            media_type: obj
                .get("media_type")
                .and_then(Value::as_str)
                .unwrap_or("image/png")
                .to_string(),
        }),
        _ => Ok(ContentPart::Text(
            obj.get("text")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
        )),
    }
}

pub fn tool_call_to_value(call: &ToolCall) -> Value {
    json!({
        "id": call.id,
        "name": call.name,
        "arguments": call.arguments,
    })
}

pub fn tool_call_from_value(value: &Value) -> Result<ToolCall, String> {
    let obj = value
        .as_object()
        .ok_or_else(|| "tool call must be an object".to_string())?;
    Ok(ToolCall {
        id: obj
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        name: obj
            .get("name")
            .and_then(Value::as_str)
            .ok_or_else(|| "tool call name required".to_string())?
            .to_string(),
        arguments: obj.get("arguments").cloned().unwrap_or_else(|| json!({})),
    })
}

pub fn tool_result_to_value(result: &ToolResult) -> Value {
    json!({
        "success": result.success,
        "error": result.error,
        "data": result.data,
        "message": result.message,
        "terminal": result.terminal,
        "needsFollowup": result.needs_followup,
    })
}

pub fn tool_result_from_value(value: &Value) -> Result<ToolResult, String> {
    let obj = value
        .as_object()
        .ok_or_else(|| "tool result must be an object".to_string())?;
    Ok(ToolResult {
        success: obj.get("success").and_then(Value::as_bool).unwrap_or(false),
        error: obj.get("error").and_then(Value::as_str).map(str::to_string),
        data: obj.get("data").cloned().filter(|v| !v.is_null()),
        message: obj
            .get("message")
            .and_then(Value::as_str)
            .map(str::to_string),
        terminal: obj
            .get("terminal")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        needs_followup: obj
            .get("needsFollowup")
            .or_else(|| obj.get("needs_followup"))
            .and_then(Value::as_bool)
            .unwrap_or(false),
    })
}

pub fn chunks_from_value(value: &Value) -> Result<Vec<LLMStreamChunk>, String> {
    value
        .as_array()
        .ok_or_else(|| "chunks must be an array".to_string())?
        .iter()
        .map(chunk_from_value)
        .collect()
}

fn chunk_from_value(value: &Value) -> Result<LLMStreamChunk, String> {
    let obj = value
        .as_object()
        .ok_or_else(|| "chunk must be an object".to_string())?;
    let tool_call_delta = obj
        .get("tool_call_delta")
        .filter(|v| !v.is_null())
        .map(tool_call_from_value)
        .transpose()?;
    Ok(LLMStreamChunk {
        content_delta: obj
            .get("content_delta")
            .and_then(Value::as_str)
            .map(str::to_string),
        reasoning_delta: obj
            .get("reasoning_delta")
            .and_then(Value::as_str)
            .map(str::to_string),
        reasoning_details: obj
            .get("reasoning_details")
            .cloned()
            .filter(|v| !v.is_null()),
        tool_call_delta,
        finish_reason: obj
            .get("finish_reason")
            .and_then(Value::as_str)
            .map(str::to_string),
        usage: obj
            .get("usage")
            .filter(|v| !v.is_null())
            .map(usage_from_value)
            .transpose()?,
    })
}

fn usage_from_value(value: &Value) -> Result<LLMUsage, String> {
    let obj = value
        .as_object()
        .ok_or_else(|| "usage must be an object".to_string())?;
    Ok(LLMUsage {
        prompt_tokens: int_field(obj, "prompt_tokens"),
        completion_tokens: int_field(obj, "completion_tokens"),
        total_tokens: int_field(obj, "total_tokens"),
        cached_prompt_tokens: int_field(obj, "cached_prompt_tokens"),
        cache_creation_tokens: int_field(obj, "cache_creation_tokens"),
    })
}

fn int_field(obj: &serde_json::Map<String, Value>, key: &str) -> i64 {
    obj.get(key).and_then(Value::as_i64).unwrap_or(0)
}

pub fn config_from_value(value: &Value) -> LoopConfig {
    let obj = value.as_object();
    let budget = obj.and_then(|o| o.get("budget")).and_then(|b| {
        let b = b.as_object()?;
        Some(BudgetLimit {
            max_tokens: b.get("max_tokens").and_then(Value::as_i64).unwrap_or(0),
            max_steps: b.get("max_steps").and_then(Value::as_i64).unwrap_or(0),
            max_tool_calls: b.get("max_tool_calls").and_then(Value::as_i64).unwrap_or(0),
            cached_token_weight: b
                .get("cached_token_weight")
                .and_then(Value::as_f64)
                .unwrap_or(crate::budget::DEFAULT_CACHED_TOKEN_WEIGHT),
        })
    });
    LoopConfig {
        max_rounds: obj
            .and_then(|o| o.get("max_rounds"))
            .and_then(Value::as_u64)
            .unwrap_or(32) as u32,
        max_tool_errors: obj
            .and_then(|o| o.get("max_tool_errors"))
            .and_then(Value::as_u64)
            .unwrap_or(3) as u32,
        budget,
        persist_tool_results: obj
            .and_then(|o| o.get("persist_tool_results"))
            .and_then(Value::as_bool)
            .unwrap_or(false),
        parallel_tools: obj
            .and_then(|o| o.get("parallel_tools"))
            .and_then(Value::as_bool)
            .unwrap_or(true),
        tool_dedup: obj
            .and_then(|o| o.get("tool_dedup"))
            .and_then(Value::as_bool)
            .unwrap_or(true),
        tool_timeout_ms: match obj.and_then(|o| o.get("tool_timeout_ms")) {
            Some(Value::Null) => None,
            Some(value) => value.as_u64(),
            None => Some(300_000),
        },
        soft_timeout_ms: obj
            .and_then(|o| o.get("soft_timeout_ms"))
            .and_then(Value::as_u64),
        wrap_up_keeps_tools: obj
            .and_then(|o| o.get("wrap_up_keeps_tools"))
            .and_then(Value::as_bool)
            .unwrap_or(false),
        wrap_up_max_tool_rounds: obj
            .and_then(|o| o.get("wrap_up_max_tool_rounds"))
            .and_then(Value::as_u64)
            .unwrap_or(4) as u32,
        wrap_up_tool_timeout_ms: obj
            .and_then(|o| o.get("wrap_up_tool_timeout_ms"))
            .and_then(Value::as_u64),
        wrap_up_hard_cap_ms: obj
            .and_then(|o| o.get("wrap_up_hard_cap_ms"))
            .and_then(Value::as_u64),
        steer_mode: obj
            .and_then(|o| o.get("steer_mode"))
            .and_then(Value::as_str)
            .unwrap_or("boundary")
            .to_string(),
    }
}

pub fn usage_to_value(usage: &LLMUsage) -> Value {
    json!({
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "cached_prompt_tokens": usage.cached_prompt_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
    })
}
