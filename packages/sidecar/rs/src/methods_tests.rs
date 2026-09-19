use super::*;

fn llm(provider: &str) -> TurnLlm {
    TurnLlm {
        provider: provider.into(),
        base_url: Some("http://localhost:11434".into()),
        api_key: Some("test".into()),
        model: Some("model".into()),
        ..TurnLlm::default()
    }
}

#[test]
fn factory_selects_native_provider_protocols() {
    assert!(matches!(
        http_provider(&llm("openai_compat")).unwrap(),
        Some(HttpProvider::OpenAI(_))
    ));
    assert!(matches!(
        http_provider(&llm("responses")).unwrap(),
        Some(HttpProvider::Responses(_))
    ));
    assert!(matches!(
        http_provider(&llm("anthropic")).unwrap(),
        Some(HttpProvider::Anthropic(_))
    ));
    assert!(matches!(
        http_provider(&llm("gemini")).unwrap(),
        Some(HttpProvider::Gemini(_))
    ));
}

#[test]
fn factory_normalizes_ollama_v1_url() {
    let Some(HttpProvider::OpenAI(provider)) = http_provider(&llm("ollama")).unwrap() else {
        panic!("ollama should use OpenAI-compatible wire");
    };
    assert_eq!(provider.base_url, "http://localhost:11434/v1");
}

#[test]
fn factory_rejects_unknown_provider() {
    assert_eq!(
        http_provider(&llm("mystery")).err().unwrap(),
        "unknown provider: \"mystery\""
    );
}

#[test]
fn chat_params_preserve_provider_generation_controls() {
    let params = json!({
        "provider": "responses",
        "model": "gpt-5",
        "baseUrl": "https://api.openai.com/v1",
        "temperature": 0.2,
        "maxTokens": 4096,
        "reasoningEffort": "high",
    });
    let resolved = turn_llm_from_params(params.as_object().unwrap());
    assert_eq!(resolved.provider, "responses");
    assert_eq!(resolved.temperature, Some(0.2));
    assert_eq!(resolved.max_tokens, Some(4096));
    assert_eq!(resolved.reasoning_effort.as_deref(), Some("high"));
}

#[tokio::test]
async fn provider_http_error_emits_stream_error_and_failed_done() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut request = vec![0_u8; 8192];
        let _ = socket.read(&mut request).await;
        let body = r#"{"error":{"message":"invalid API key"}}"#;
        let response = format!(
            "HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        socket.write_all(response.as_bytes()).await.unwrap();
    });

    let notifications = run_turn(
        vec![LLMMessage::text("user", "hello")],
        "stream-error".into(),
        TurnLlm {
            provider: "openai_compat".into(),
            base_url: Some(format!("http://{addr}/v1")),
            api_key: Some("invalid".into()),
            model: Some("test".into()),
            ..TurnLlm::default()
        },
    )
    .await;

    let stream_error = notifications
        .iter()
        .find(|item| item["method"] == "stream.error")
        .unwrap();
    assert_eq!(
        stream_error["params"]["message"],
        "openai_compat: HTTP 401 (auth): {\"error\":{\"message\":\"invalid API key\"}}"
    );
    let completion = notifications
        .iter()
        .find(|item| {
            item["method"] == "stream.chunk" && item["params"]["notice"]["kind"] == "completion"
        })
        .unwrap();
    assert_eq!(completion["params"]["notice"]["data"]["status"], "failed");
    assert_eq!(
        notifications.last().unwrap(),
        &json!({
            "jsonrpc": "2.0",
            "method": "stream.done",
            "params": {
                "streamId": "stream-error",
                "ok": false,
                "engine": "rust",
            },
        })
    );
}
