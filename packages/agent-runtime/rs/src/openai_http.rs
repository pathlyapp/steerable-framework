//! HTTP streaming for OpenAI-compatible `/chat/completions`.

use futures_util::StreamExt;
use serde_json::{Map, Value};

use crate::errors::{classify_http_status, parse_retry_after_ms, LLMError, LLMErrorKind};
use crate::openai_wire::{consume_sse_lines, stream_timeout, OpenAICompatProvider};
use crate::provider::LLMStreamChunk;
use crate::types::LLMMessage;

fn http_error(provider: &str, status: u16, body_text: &str, retry_after: Option<&str>) -> LLMError {
    let kind = classify_http_status(status, body_text);
    let snippet: String = body_text
        .trim()
        .chars()
        .map(|ch| if ch == '\n' { ' ' } else { ch })
        .take(300)
        .collect();
    let mut message = format!("{provider}: HTTP {status} ({})", kind.as_str());
    if !snippet.is_empty() {
        message.push_str(": ");
        message.push_str(&snippet);
    }
    LLMError {
        message,
        kind,
        status_code: Some(status),
        provider: Some(provider.to_string()),
        retry_after_ms: retry_after.and_then(parse_retry_after_ms),
    }
}

impl OpenAICompatProvider {
    pub async fn stream(
        &self,
        messages: &[LLMMessage],
        tools: Option<&[Value]>,
        extra: &Map<String, Value>,
    ) -> Result<Vec<LLMStreamChunk>, LLMError> {
        let body = self.build_body(messages, tools, None, None, true, extra);
        let (connect, read) = stream_timeout();
        let mut header_map = reqwest::header::HeaderMap::new();
        for (key, value) in self.request_headers() {
            let name = reqwest::header::HeaderName::from_bytes(key.as_bytes()).map_err(|err| {
                LLMError::new(
                    format!("{}: {err}", self.name),
                    LLMErrorKind::InvalidRequest,
                )
            })?;
            let value = reqwest::header::HeaderValue::from_str(&value).map_err(|err| {
                LLMError::new(
                    format!("{}: {err}", self.name),
                    LLMErrorKind::InvalidRequest,
                )
            })?;
            header_map.append(name, value);
        }
        let client = reqwest::Client::builder()
            .connect_timeout(std::time::Duration::from_secs_f64(connect))
            .read_timeout(std::time::Duration::from_secs_f64(read))
            .build()
            .map_err(|err| {
                LLMError::new(
                    format!("{}: transport error: {err}", self.name),
                    LLMErrorKind::Transport,
                )
            })?;
        let url = format!("{}/chat/completions", self.base_url.trim_end_matches('/'));
        let response = client
            .post(url)
            .headers(header_map)
            .json(&body)
            .send()
            .await
            .map_err(|err| {
                LLMError::new(
                    format!("{}: transport error: {err}", self.name),
                    LLMErrorKind::Transport,
                )
            })?;
        let status = response.status().as_u16();
        if !response.status().is_success() {
            let retry_after = response
                .headers()
                .get("retry-after")
                .and_then(|value| value.to_str().ok())
                .map(str::to_string);
            let body_text = response.text().await.unwrap_or_default();
            return Err(http_error(
                &self.name,
                status,
                &body_text,
                retry_after.as_deref(),
            ));
        }
        let mut bytes = Vec::new();
        let mut stream = response.bytes_stream();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(|err| {
                LLMError::new(
                    format!("{}: transport error: {err}", self.name),
                    LLMErrorKind::Transport,
                )
            })?;
            bytes.extend_from_slice(&chunk);
        }
        let text = String::from_utf8_lossy(&bytes);
        Ok(consume_sse_lines(text.lines(), &self.compat))
    }
}
