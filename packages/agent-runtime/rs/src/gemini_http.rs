//! HTTP streaming for Google Gemini native API.

use futures_util::StreamExt;
use serde_json::{Map, Value};

use crate::errors::{classify_http_status, LLMError, LLMErrorKind};
use crate::gemini_wire::{parse_gemini_chunk, GoogleGenAIProvider};
use crate::openai_wire::stream_timeout;
use crate::provider::LLMStreamChunk;
use crate::types::LLMMessage;

impl GoogleGenAIProvider {
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
        let mut request = client.post(self.stream_url()).json(&body);
        if let Some(key) = self.api_key.as_deref().filter(|key| !key.is_empty()) {
            request = request.header("x-goog-api-key", key);
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
        for line in String::from_utf8_lossy(&bytes).lines() {
            let line = line.trim();
            if line.is_empty() || line.starts_with(':') {
                continue;
            }
            let line = line.strip_prefix("data:").map(str::trim).unwrap_or(line);
            if line == "[DONE]" {
                break;
            }
            if let Ok(value) = serde_json::from_str::<Value>(line) {
                out.extend(parse_gemini_chunk(&value));
            }
        }
        Ok(out)
    }
}
