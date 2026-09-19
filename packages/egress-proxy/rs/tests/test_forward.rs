use std::sync::Arc;
use std::time::Duration;

use steerable_egress_proxy::{
    parse_and_rewrite_request, AllowList, EgressProxyServer, InjectRule, ProxyConfig,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::Mutex;

fn rule() -> InjectRule {
    InjectRule::new(
        "127.0.0.1",
        "Bearer test-key",
        "Authorization",
        "http",
        None,
    )
    .unwrap()
}

#[test]
fn rewrite_strips_client_credential_and_injects_rule_secret() {
    let head = b"POST http://127.0.0.1/v1/chat/completions?stream=true HTTP/1.1\r\n\
Host: 127.0.0.1\r\n\
Authorization: Bearer client-side-fake\r\n\
Proxy-Authorization: Basic abc\r\n\
Content-Type: application/json\r\n\
Content-Length: 11\r\n\
Connection: keep-alive\r\n\r\n";
    let parsed = parse_and_rewrite_request(head, &rule()).unwrap();
    let text = String::from_utf8(parsed.head.clone()).unwrap();
    assert!(text.starts_with("POST /v1/chat/completions?stream=true HTTP/1.1\r\n"));
    assert!(text.contains("Authorization: Bearer test-key\r\n"));
    assert!(!text.contains("client-side-fake"));
    assert!(!text.contains("Proxy-Authorization"));
    assert!(!text.contains("keep-alive"));
    assert!(text.contains("Content-Length: 11\r\n"));
    assert_eq!(parsed.body_remaining, 11);
}

#[test]
fn rewrite_rejects_off_host_and_non_http_targets() {
    assert_eq!(
        parse_and_rewrite_request(b"POST http://evil.example.com/v1 HTTP/1.1\r\n\r\n", &rule())
            .unwrap_err(),
        "403"
    );
    assert_eq!(
        parse_and_rewrite_request(b"POST /v1/origin-form HTTP/1.1\r\n\r\n", &rule()).unwrap_err(),
        "403"
    );
}

#[test]
fn rewrite_flags_chunked_request_for_501() {
    let parsed = parse_and_rewrite_request(
        b"POST http://127.0.0.1/v1 HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n",
        &rule(),
    )
    .unwrap();
    assert!(parsed.chunked);
}

#[test]
fn inject_rule_requires_host_and_secret() {
    assert!(InjectRule::new("", "x", "Authorization", "https", None).is_err());
    assert!(InjectRule::new("h", "", "Authorization", "https", None).is_err());
    assert!(InjectRule::new("h", "x", "Authorization", "gopher", None).is_err());
}

async fn start_upstream() -> (u16, Arc<Mutex<Vec<Vec<u8>>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let captured = Arc::new(Mutex::new(Vec::new()));
    let store = Arc::clone(&captured);
    tokio::spawn(async move {
        let Ok((mut stream, _)) = listener.accept().await else {
            return;
        };
        let mut buf = Vec::new();
        let mut tmp = [0u8; 4096];
        loop {
            let n = match stream.read(&mut tmp).await {
                Ok(0) | Err(_) => break,
                Ok(n) => n,
            };
            buf.extend_from_slice(&tmp[..n]);
            if buf.windows(4).any(|w| w == b"\r\n\r\n") {
                if let Some(length) = content_length(&buf) {
                    if let Some(pos) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                        if buf.len() >= pos + 4 + length {
                            break;
                        }
                    }
                }
            }
        }
        store.lock().await.push(buf);
        let _ = stream
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            .await;
        let _ = stream.shutdown().await;
    });
    (port, captured)
}

fn content_length(buf: &[u8]) -> Option<usize> {
    let text = String::from_utf8_lossy(buf);
    for line in text.split("\r\n") {
        if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
            return value.trim().parse().ok();
        }
    }
    None
}

async fn start_proxy(inject: Option<InjectRule>, record: Option<String>) -> u16 {
    let mut server = EgressProxyServer::new(ProxyConfig {
        allow: Arc::new(Mutex::new(AllowList::new(&["127.0.0.1".into()]).unwrap())),
        bind_host: "127.0.0.1".into(),
        bind_port: 0,
        connect_timeout: Duration::from_secs(2),
        inject,
        record_requests: record,
        control_token: None,
        control_port: 0,
    });
    let port = server.bind().await.unwrap();
    tokio::spawn(async move {
        let _ = server.serve().await;
    });
    port
}

async fn raw_request(proxy_port: u16, payload: &[u8]) -> Vec<u8> {
    let mut stream = TcpStream::connect(("127.0.0.1", proxy_port)).await.unwrap();
    stream.write_all(payload).await.unwrap();
    let _ = stream.shutdown().await;
    let mut buf = Vec::new();
    stream.read_to_end(&mut buf).await.unwrap();
    buf
}

#[tokio::test]
async fn forward_injects_and_streams_response() {
    let (upstream_port, captured) = start_upstream().await;
    let inject = InjectRule::new(
        "127.0.0.1",
        "Bearer test-key",
        "Authorization",
        "http",
        Some(upstream_port),
    )
    .unwrap();
    let proxy = start_proxy(Some(inject), None).await;
    let body = b"{\"msg\":\"hi\"}";
    let mut payload = format!(
        "POST http://127.0.0.1/v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer should-never-arrive\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n",
        body.len()
    )
    .into_bytes();
    payload.extend_from_slice(body);
    let response = raw_request(proxy, &payload).await;
    assert!(String::from_utf8_lossy(&response).starts_with("HTTP/1.1 200 OK"));
    assert!(response.ends_with(b"ok"));
    let received = captured.lock().await.pop().expect("upstream saw a request");
    let text = String::from_utf8_lossy(&received);
    assert!(text.contains("Authorization: Bearer test-key"));
    assert!(!text.contains("should-never-arrive"));
    assert!(received.ends_with(body));
}

#[tokio::test]
async fn forward_tees_request_body_without_the_secret() {
    let (upstream_port, _) = start_upstream().await;
    let inject = InjectRule::new(
        "127.0.0.1",
        "Bearer test-key",
        "Authorization",
        "http",
        Some(upstream_port),
    )
    .unwrap();
    let record = std::env::temp_dir().join(format!("egress-tee-{}.jsonl", std::process::id()));
    let _ = std::fs::remove_file(&record);
    let proxy = start_proxy(Some(inject), Some(record.to_string_lossy().into_owned())).await;
    let body = b"{\"msg\":\"hi\"}";
    let mut payload = format!(
        "POST http://127.0.0.1/v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n",
        body.len()
    )
    .into_bytes();
    payload.extend_from_slice(body);
    let response = raw_request(proxy, &payload).await;
    assert!(String::from_utf8_lossy(&response).starts_with("HTTP/1.1 200 OK"));
    let line: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&record).unwrap()).unwrap();
    assert_eq!(line["method"], "POST");
    assert_eq!(line["path"], "/v1/messages");
    assert_eq!(line["body"], "{\"msg\":\"hi\"}");
    assert!(!std::fs::read_to_string(&record)
        .unwrap()
        .contains("test-key"));
    let _ = std::fs::remove_file(&record);
}

#[tokio::test]
async fn forward_denies_off_host_with_403() {
    let proxy = start_proxy(Some(rule()), None).await;
    let response = raw_request(
        proxy,
        b"GET http://not-the-rule-host.example/ HTTP/1.1\r\n\r\n",
    )
    .await;
    assert!(String::from_utf8_lossy(&response).starts_with("HTTP/1.1 403"));
}

#[tokio::test]
async fn plain_http_stays_405_without_inject_rule() {
    let proxy = start_proxy(None, None).await;
    let response = raw_request(proxy, b"GET http://127.0.0.1/ HTTP/1.1\r\n\r\n").await;
    assert!(String::from_utf8_lossy(&response).starts_with("HTTP/1.1 405"));
}

#[tokio::test]
async fn chunked_request_is_501() {
    let proxy = start_proxy(Some(rule()), None).await;
    let response = raw_request(
        proxy,
        b"POST http://127.0.0.1/v1 HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n",
    )
    .await;
    assert!(String::from_utf8_lossy(&response).starts_with("HTTP/1.1 501"));
}
