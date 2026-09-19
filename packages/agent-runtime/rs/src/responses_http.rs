//! HTTP streaming for the OpenAI Responses API.

use futures_util::StreamExt;
use serde_json::{Map, Value};

use crate::errors::{classify_http_status, LLMError, LLMErrorKind};
use crate::openai_wire::stream_timeout;
use crate::provider::LLMStreamChunk;
use crate::responses_wire::{
    parse_responses_event, OpenAIResponsesProvider, ResponsesToolCallAssembler,
};
use crate::types::LLMMessage;

impl OpenAIResponsesProvider {
    pub async fn stream(
        &self,
        messages: &[LLMMessage],
        tools: Option<&[Value]>,
        extra: &Map<String, Value>,
    ) -> Result<Vec<LLMStreamChunk>, LLMError> {
        let body = self.build_body(messages, tools, None, None, true, extra);
        let (connect, read) = stream_timeout();
        let client = reqwest::Client::builder()
            .connect_timeout(std::time::Duration::from_secs_f64(connect))
            .read_timeout(std::time::Duration::from_secs_f64(read))
            .build()
            .map_err(|error| {
                LLMError::new(
                    format!("{}: transport error: {error}", self.name),
                    LLMErrorKind::Transport,
                )
            })?;
        let mut request = client
            .post(format!("{}/responses", self.base_url.trim_end_matches('/')))
            .json(&body);
        for (name, value) in self.request_headers() {
            request = request.header(name, value);
        }
        let response = request.send().await.map_err(|error| {
            LLMError::new(
                format!("{}: transport error: {error}", self.name),
                LLMErrorKind::Transport,
            )
        })?;
        if !response.status().is_success() {
            let status = response.status().as_u16();
            let text = response.text().await.unwrap_or_default();
            return Err(LLMError {
                message: format!("{}: HTTP {status}: {text}", self.name),
                kind: classify_http_status(status, &text),
                status_code: Some(status),
                provider: Some(self.name.clone()),
                retry_after_ms: None,
            });
        }
        let mut bytes = Vec::new();
        let mut stream = response.bytes_stream();
        while let Some(chunk) = stream.next().await {
            bytes.extend_from_slice(&chunk.map_err(|error| {
                LLMError::new(
                    format!("{}: transport error: {error}", self.name),
                    LLMErrorKind::Transport,
                )
            })?);
        }
        let mut out = Vec::new();
        let mut calls = ResponsesToolCallAssembler::default();
        for line in String::from_utf8_lossy(&bytes).lines() {
            let line = line.trim();
            if line.is_empty() || line.starts_with(':') {
                continue;
            }
            let line = line.strip_prefix("data:").map(str::trim).unwrap_or(line);
            if line == "[DONE]" {
                break;
            }
            let Ok(event) = serde_json::from_str::<Value>(line) else {
                continue;
            };
            calls.observe(&event);
            if let Some(chunk) = parse_responses_event(&event) {
                out.push(chunk);
            }
        }
        out.extend(calls.flush().into_iter().map(|call| LLMStreamChunk {
            tool_call_delta: Some(call),
            ..LLMStreamChunk::default()
        }));
        Ok(out)
    }
}
