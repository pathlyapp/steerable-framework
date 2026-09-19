//! HTTP streaming for Anthropic native `/v1/messages`.

use std::collections::BTreeMap;

use futures_util::StreamExt;
use serde_json::{json, Map, Value};

use crate::anthropic_wire::{parse_anthropic_event, AnthropicProvider};
use crate::errors::{classify_http_status, LLMError, LLMErrorKind};
use crate::openai_wire::stream_timeout;
use crate::provider::LLMStreamChunk;
use crate::types::{LLMMessage, ToolCall};

impl AnthropicProvider {
    pub async fn stream(
        &self,
        messages: &[LLMMessage],
        tools: Option<&[Value]>,
        extra: &Map<String, Value>,
    ) -> Result<Vec<LLMStreamChunk>, LLMError> {
        let body = self.build_body(messages, tools, None, None, extra);
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
            .post(format!(
                "{}/v1/messages",
                self.base_url.trim_end_matches('/')
            ))
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
        let mut calls: BTreeMap<u64, (String, String, String)> = BTreeMap::new();
        for line in String::from_utf8_lossy(&bytes).lines() {
            let line = line.trim();
            if line.is_empty() || line.starts_with(':') || line.starts_with("event:") {
                continue;
            }
            let line = line.strip_prefix("data:").map(str::trim).unwrap_or(line);
            let Ok(event) = serde_json::from_str::<Value>(line) else {
                continue;
            };
            let index = event.get("index").and_then(Value::as_u64).unwrap_or(0);
            match event.get("type").and_then(Value::as_str) {
                Some("content_block_start")
                    if event.pointer("/content_block/type").and_then(Value::as_str)
                        == Some("tool_use") =>
                {
                    let block = event.get("content_block").unwrap_or(&Value::Null);
                    calls.insert(
                        index,
                        (
                            block
                                .get("id")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_string(),
                            block
                                .get("name")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_string(),
                            String::new(),
                        ),
                    );
                }
                Some("content_block_delta")
                    if event.pointer("/delta/type").and_then(Value::as_str)
                        == Some("input_json_delta") =>
                {
                    calls.entry(index).or_default().2.push_str(
                        event
                            .pointer("/delta/partial_json")
                            .and_then(Value::as_str)
                            .unwrap_or(""),
                    );
                }
                Some("content_block_stop") => {
                    if let Some((id, name, arguments)) = calls.remove(&index) {
                        out.push(LLMStreamChunk {
                            tool_call_delta: Some(ToolCall {
                                id,
                                name,
                                arguments: serde_json::from_str(&arguments)
                                    .ok()
                                    .filter(Value::is_object)
                                    .unwrap_or_else(|| json!({})),
                            }),
                            ..LLMStreamChunk::default()
                        });
                    }
                }
                _ => {
                    if let Some(chunk) = parse_anthropic_event(&event) {
                        out.push(chunk);
                    }
                }
            }
        }
        Ok(out)
    }
}
