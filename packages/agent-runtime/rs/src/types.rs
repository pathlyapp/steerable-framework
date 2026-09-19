use serde_json::{json, Map, Value};

use crate::json_py::dumps;

#[derive(Clone, Debug, Default)]
pub struct LoopContext {
    pub consecutive_tool_errors: u32,
    pub tool_successes: u32,
    pub tool_calls_used: u32,
    pub last_prompt_tokens: i64,
    pub last_prompt_transcript_len: usize,
    pub last_cached_prompt_tokens: i64,
    pub last_cache_creation_tokens: i64,
    pub accumulated_prompt_tokens: i64,
    pub accumulated_completion_tokens: i64,
}

pub const MAX_COMPLETION_REDOS: u32 = 32;
pub const MAX_READ_IMAGES_PER_ROUND: usize = 4;

#[derive(Clone, Debug, PartialEq)]
pub struct LoopEvent {
    pub kind: String,
    pub data: Value,
}

impl LoopEvent {
    pub fn new(kind: impl Into<String>, data: Value) -> Self {
        Self {
            kind: kind.into(),
            data,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    pub arguments: Value,
}

impl ToolCall {
    pub fn new(name: impl Into<String>, arguments: Value) -> Self {
        let name = name.into();
        Self {
            id: format!("call_{name}"),
            name,
            arguments,
        }
    }

    pub fn with_id(mut self, id: impl Into<String>) -> Self {
        self.id = id.into();
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ToolResult {
    pub success: bool,
    pub error: Option<String>,
    pub data: Option<Value>,
    pub message: Option<String>,
    pub terminal: bool,
    pub needs_followup: bool,
}

impl ToolResult {
    pub fn ok(data: Value) -> Self {
        Self {
            success: true,
            error: None,
            data: Some(data),
            message: None,
            terminal: false,
            needs_followup: false,
        }
    }

    pub fn fail(error: impl Into<String>) -> Self {
        Self {
            success: false,
            error: Some(error.into()),
            data: None,
            message: None,
            terminal: false,
            needs_followup: true,
        }
    }

    pub fn fail_with_data(error: impl Into<String>, data: Value) -> Self {
        Self {
            success: false,
            error: Some(error.into()),
            data: Some(data),
            message: None,
            terminal: false,
            needs_followup: true,
        }
    }

    pub fn fail_closed(error: impl Into<String>) -> Self {
        Self {
            success: false,
            error: Some(error.into()),
            data: None,
            message: None,
            terminal: false,
            needs_followup: false,
        }
    }

    pub fn fail_closed_data(error: impl Into<String>, data: Value) -> Self {
        Self {
            success: false,
            error: Some(error.into()),
            data: Some(data),
            message: None,
            terminal: false,
            needs_followup: false,
        }
    }

    pub fn to_rpc(&self) -> Value {
        json!({
            "success": self.success,
            "error": self.error,
            "data": self.data,
            "message": self.message,
            "terminal": self.terminal,
            "needsFollowup": self.needs_followup,
        })
    }

    pub fn result_content(&self) -> String {
        let mut payload = Map::new();
        payload.insert("success".into(), json!(self.success));
        if let Some(error) = &self.error {
            if !error.is_empty() {
                payload.insert("error".into(), json!(error));
            }
        }
        if let Some(data) = &self.data {
            payload.insert("data".into(), data.clone());
        }
        if let Some(message) = &self.message {
            if !message.is_empty() {
                payload.insert("message".into(), json!(message));
            }
        }
        dumps(&Value::Object(payload))
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum ContentPart {
    Text(String),
    Image { source: String, media_type: String },
}

impl ContentPart {
    pub fn is_image(&self) -> bool {
        matches!(self, Self::Image { .. })
    }

    pub fn image_source(&self) -> Option<&str> {
        match self {
            Self::Image { source, .. } => Some(source),
            Self::Text(_) => None,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct LLMMessage {
    pub role: String,
    pub content: Vec<ContentPart>,
    pub name: Option<String>,
    pub tool_call_id: Option<String>,
    pub tool_calls: Vec<ToolCall>,
    pub reasoning: Option<String>,
    pub reasoning_details: Option<Value>,
}

impl LLMMessage {
    pub fn text(role: impl Into<String>, text: impl Into<String>) -> Self {
        Self {
            role: role.into(),
            content: vec![ContentPart::Text(text.into())],
            name: None,
            tool_call_id: None,
            tool_calls: Vec::new(),
            reasoning: None,
            reasoning_details: None,
        }
    }

    pub fn tool(call: &ToolCall, result: &ToolResult) -> Self {
        Self {
            role: "tool".into(),
            content: vec![ContentPart::Text(result.result_content())],
            name: Some(call.name.clone()),
            tool_call_id: Some(call.id.clone()),
            tool_calls: Vec::new(),
            reasoning: None,
            reasoning_details: None,
        }
    }

    pub fn content_text(&self) -> String {
        self.content
            .iter()
            .filter_map(|part| match part {
                ContentPart::Text(text) => Some(text.as_str()),
                ContentPart::Image { .. } => None,
            })
            .collect()
    }

    pub fn has_image(&self) -> bool {
        self.content.iter().any(ContentPart::is_image)
    }
}
