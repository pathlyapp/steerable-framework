use std::sync::Arc;
use std::time::Duration;

use steerable_egress_proxy::{AllowList, EgressProxyServer, ProxyConfig};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::Mutex;

#[test]
fn runtime_addition_widens_allows() {
    let mut allow = AllowList::new(&["api.github.com".into()]).unwrap();
    assert!(!allow.allows("example.com", 443));
    allow.add("example.com").unwrap();
    assert!(allow.allows("example.com", 443));
    assert!(allow.allows("example.com", 80));
    assert!(!allow.allows("example.com", 8080));
}

#[test]
fn baseline_entries_stay_immutable() {
    let mut allow = AllowList::new(&["api.github.com".into()]).unwrap();
    allow.add("example.com:8443").unwrap();
    assert_eq!(
        allow
            .entries()
            .iter()
            .map(|e| e.host.as_str())
            .collect::<Vec<_>>(),
        vec!["api.github.com"]
    );
}

#[test]
fn add_validates_like_baseline() {
    let mut allow = AllowList::new(&["api.github.com".into()]).unwrap();
    assert!(allow.add("bad host!").is_err());
}

async fn start_proxy(token: Option<&str>) -> (u16, Option<u16>) {
    let mut server = EgressProxyServer::new(ProxyConfig {
        allow: Arc::new(Mutex::new(
            AllowList::new(&["api.github.com".into()]).unwrap(),
        )),
        bind_host: "127.0.0.1".into(),
        bind_port: 0,
        connect_timeout: Duration::from_secs(2),
        inject: None,
        record_requests: None,
        control_token: token.map(str::to_string),
        control_port: 0,
    });
    let port = server.bind().await.unwrap();
    let control = server.bind_control().await.unwrap();
    tokio::spawn(async move {
        let _ = server.serve().await;
    });
    (port, control)
}

async fn connect_request(port: u16, target: &str) -> Vec<u8> {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).await.unwrap();
    stream
        .write_all(format!("CONNECT {target} HTTP/1.1\r\n\r\n").as_bytes())
        .await
        .unwrap();
    let mut head = vec![0u8; 256];
    let n = stream.read(&mut head).await.unwrap();
    head.truncate(n);
    head
}

async fn control_request(port: u16, raw: &[u8]) -> Vec<u8> {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).await.unwrap();
    stream.write_all(raw).await.unwrap();
    let mut buf = vec![0u8; 1024];
    let n = stream.read(&mut buf).await.unwrap_or(0);
    buf.truncate(n);
    buf
}

fn allow_post(token: &str, host: &str) -> Vec<u8> {
    let body = format!(r#"{{"host":"{host}"}}"#);
    format!(
        "POST /allow HTTP/1.1\r\nauthorization: Bearer {token}\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{body}",
        body.len()
    )
    .into_bytes()
}

#[tokio::test]
async fn denied_reason_names_target() {
    let (port, _) = start_proxy(None).await;
    let head = connect_request(port, "evil.example.com:443").await;
    assert!(String::from_utf8_lossy(&head)
        .starts_with("HTTP/1.1 403 Forbidden; egress denied for evil.example.com:443"));
}

#[tokio::test]
async fn control_allow_widens_session_list() {
    let (port, control) = start_proxy(Some("s3cret")).await;
    let control = control.expect("control port");
    let denied = connect_request(port, "192.0.2.1:443").await;
    assert!(String::from_utf8_lossy(&denied).starts_with("HTTP/1.1 403"));
    let granted = control_request(control, &allow_post("s3cret", "192.0.2.1")).await;
    let text = String::from_utf8_lossy(&granted);
    assert!(text.starts_with("HTTP/1.1 200"));
    assert!(text.contains("192.0.2.1"));
    let allowed = connect_request(port, "192.0.2.1:443").await;
    assert!(String::from_utf8_lossy(&allowed).starts_with("HTTP/1.1 502"));
}

#[tokio::test]
async fn wrong_token_is_unauthorized() {
    let (_, control) = start_proxy(Some("s3cret")).await;
    let control = control.expect("control port");
    let head = control_request(control, &allow_post("nope", "example.com")).await;
    assert!(String::from_utf8_lossy(&head).starts_with("HTTP/1.1 401"));
}

#[tokio::test]
async fn no_token_configured_means_no_control_plane() {
    let (_, control) = start_proxy(None).await;
    assert!(control.is_none());
}
