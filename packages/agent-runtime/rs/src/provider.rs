use async_trait::async_trait;
use serde_json::Value;

use crate::errors::LLMError;
use crate::types::{LLMMessage, ToolCall};

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct LLMUsage {
    pub prompt_tokens: i64,
    pub completion_tokens: i64,
    pub total_tokens: i64,
    pub cached_prompt_tokens: i64,
    pub cache_creation_tokens: i64,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct LLMStreamChunk {
    pub content_delta: Option<String>,
    pub reasoning_delta: Option<String>,
    pub reasoning_details: Option<Value>,
    pub tool_call_delta: Option<ToolCall>,
    pub finish_reason: Option<String>,
    pub usage: Option<LLMUsage>,
}

#[async_trait]
pub trait LLMProvider: Send {
    fn name(&self) -> &str;
    fn model(&self) -> &str;
    async fn stream(&mut self, messages: &[LLMMessage]) -> Result<Vec<LLMStreamChunk>, LLMError>;
    async fn stream_with_tools(
        &mut self,
        messages: &[LLMMessage],
        _tools: &[Value],
    ) -> Result<Vec<LLMStreamChunk>, LLMError> {
        self.stream(messages).await
    }

    async fn stream_with_options(
        &mut self,
        messages: &[LLMMessage],
        tools: &[Value],
        _tool_choice: Option<&str>,
    ) -> Result<Vec<LLMStreamChunk>, LLMError> {
        self.stream_with_tools(messages, tools).await
    }
}

#[derive(Clone, Debug, Default)]
pub struct ScriptedTurn {
    pub content: String,
    pub tool_calls: Vec<ToolCall>,
    pub reasoning: Option<String>,
    pub reasoning_details: Option<Value>,
    pub usage: Option<LLMUsage>,
}

/// Plays back one scripted turn per `stream()` call; extra calls replay the last.
#[derive(Debug)]
pub struct ScriptedProvider {
    pub calls: Vec<Vec<LLMMessage>>,
    scripts: Vec<ScriptedTurn>,
    idx: usize,
}

impl ScriptedProvider {
    pub fn new(scripts: Vec<ScriptedTurn>) -> Self {
        Self {
            calls: Vec::new(),
            scripts,
            idx: 0,
        }
    }
}

#[async_trait]
impl LLMProvider for ScriptedProvider {
    fn name(&self) -> &str {
        "fake"
    }

    fn model(&self) -> &str {
        "fake-model"
    }

    async fn stream(&mut self, messages: &[LLMMessage]) -> Result<Vec<LLMStreamChunk>, LLMError> {
        self.calls.push(messages.to_vec());
        let entry = &self.scripts[self.idx.min(self.scripts.len().saturating_sub(1))];
        self.idx += 1;
        let mut chunks = Vec::new();
        if !entry.content.is_empty() {
            chunks.push(LLMStreamChunk {
                content_delta: Some(entry.content.clone()),
                ..LLMStreamChunk::default()
            });
        }
        if let Some(reasoning) = &entry.reasoning {
            chunks.push(LLMStreamChunk {
                reasoning_delta: Some(reasoning.clone()),
                ..LLMStreamChunk::default()
            });
        }
        if let Some(details) = &entry.reasoning_details {
            chunks.push(LLMStreamChunk {
                reasoning_details: Some(details.clone()),
                ..LLMStreamChunk::default()
            });
        }
        for call in &entry.tool_calls {
            chunks.push(LLMStreamChunk {
                tool_call_delta: Some(call.clone()),
                ..LLMStreamChunk::default()
            });
        }
        chunks.push(LLMStreamChunk {
            finish_reason: Some(if entry.tool_calls.is_empty() {
                "stop".into()
            } else {
                "tool_calls".into()
            }),
            usage: entry.usage.clone(),
            ..LLMStreamChunk::default()
        });
        Ok(chunks)
    }
}
