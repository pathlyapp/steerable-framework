use std::sync::Arc;
use std::time::Duration;

use steerable_egress_proxy::{
    parse_allow_entry, AllowList, EgressProxyServer, ProxyConfig, MAX_HEAD_BYTES,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::Mutex;

async fn start_echo_port() -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        loop {
            let Ok((mut stream, _)) = listener.accept().await else {
                break;
            };
            tokio::spawn(async move {
                let mut buf = vec![0u8; 4096];
                loop {
                    let n = match stream.read(&mut buf).await {
                        Ok(0) | Err(_) => break,
                        Ok(n) => n,
                    };
                    if stream.write_all(&buf[..n]).await.is_err() {
                        break;
                    }
                }
            });
        }
    });
    port
}

async fn start_proxy(allow: &[&str]) -> u16 {
    let entries: Vec<String> = allow.iter().map(|s| (*s).to_string()).collect();
    let mut server = EgressProxyServer::new(ProxyConfig {
        allow: Arc::new(Mutex::new(AllowList::new(&entries).unwrap())),
        bind_host: "127.0.0.1".into(),
        bind_port: 0,
        connect_timeout: Duration::from_secs(2),
        inject: None,
        record_requests: None,
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
    let mut buf = vec![0u8; 4096];
    let n = stream.read(&mut buf).await.unwrap();
    buf[..n].to_vec()
}

#[test]
fn bare_host_allows_443_and_80() {
    let entry = parse_allow_entry("api.deepseek.com").unwrap();
    assert!(entry.allows("api.deepseek.com", 443));
    assert!(entry.allows("api.deepseek.com", 80));
    assert!(!entry.allows("api.deepseek.com", 22));
}

#[test]
fn host_port_entry_is_exact() {
    let entry = parse_allow_entry("localhost:11434").unwrap();
    assert!(entry.allows("localhost", 11434));
    assert!(!entry.allows("localhost", 443));
}

#[test]
fn entry_matching_is_case_insensitive() {
    let entry = parse_allow_entry("API.DeepSeek.com").unwrap();
    assert!(entry.allows("api.deepseek.com", 443));
}

#[test]
fn malformed_entries_raise() {
    for raw in ["", "host:abc", "host:99999", "bad host"] {
        assert!(parse_allow_entry(raw).is_err(), "{raw}");
    }
}

#[test]
fn empty_allow_list_fails_loud() {
    assert!(AllowList::new(&[]).is_err());
}

#[tokio::test]
async fn allowed_connect_tunnels_bytes_both_ways() {
    let echo = start_echo_port().await;
    let proxy = start_proxy(&[&format!("127.0.0.1:{echo}")]).await;
    let mut stream = TcpStream::connect(("127.0.0.1", proxy)).await.unwrap();
    stream
        .write_all(
            format!("CONNECT 127.0.0.1:{echo} HTTP/1.1\r\nhost: 127.0.0.1:{echo}\r\n\r\n")
                .as_bytes(),
        )
        .await
        .unwrap();
    let mut head = vec![0u8; 128];
    let n = stream.read(&mut head).await.unwrap();
    assert!(std::str::from_utf8(&head[..n])
        .unwrap()
        .starts_with("HTTP/1.1 200"));
    stream.write_all(b"ping").await.unwrap();
    let mut buf = [0u8; 4];
    stream.read_exact(&mut buf).await.unwrap();
    assert_eq!(&buf, b"ping");
}

#[tokio::test]
async fn denied_host_gets_403_and_no_dial() {
    let proxy = start_proxy(&["api.deepseek.com"]).await;
    let head = raw_request(
        proxy,
        b"CONNECT 127.0.0.1:9 HTTP/1.1\r\nhost: 127.0.0.1:9\r\n\r\n",
    )
    .await;
    let text = String::from_utf8_lossy(&head);
    assert!(text.starts_with("HTTP/1.1 403"));
    assert!(text.contains("127.0.0.1:9"));
}

#[tokio::test]
async fn denied_port_gets_403() {
    let proxy = start_proxy(&["127.0.0.1:443"]).await;
    let head = raw_request(
        proxy,
        b"CONNECT 127.0.0.1:80 HTTP/1.1\r\nhost: 127.0.0.1:80\r\n\r\n",
    )
    .await;
    assert!(String::from_utf8_lossy(&head).starts_with("HTTP/1.1 403"));
}

#[tokio::test]
async fn non_connect_method_gets_405() {
    let proxy = start_proxy(&["127.0.0.1"]).await;
    let head = raw_request(
        proxy,
        b"GET http://127.0.0.1/ HTTP/1.1\r\nhost: 127.0.0.1\r\n\r\n",
    )
    .await;
    assert!(String::from_utf8_lossy(&head).starts_with("HTTP/1.1 405"));
}

#[tokio::test]
async fn malformed_request_gets_400() {
    let proxy = start_proxy(&["127.0.0.1"]).await;
    let head = raw_request(proxy, b"NOTHTTP\r\n\r\n").await;
    assert!(String::from_utf8_lossy(&head).starts_with("HTTP/1.1 400"));
}

#[tokio::test]
async fn oversized_head_gets_431() {
    let proxy = start_proxy(&["127.0.0.1"]).await;
    let mut payload = b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n".to_vec();
    payload.extend(vec![b'X'; MAX_HEAD_BYTES]);
    let head = raw_request(proxy, &payload).await;
    assert!(String::from_utf8_lossy(&head).starts_with("HTTP/1.1 431"));
}

#[tokio::test]
async fn unreachable_allowed_target_gets_502() {
    let proxy = start_proxy(&["127.0.0.1:1"]).await;
    let head = raw_request(
        proxy,
        b"CONNECT 127.0.0.1:1 HTTP/1.1\r\nhost: 127.0.0.1:1\r\n\r\n",
    )
    .await;
    assert!(String::from_utf8_lossy(&head).starts_with("HTTP/1.1 502"));
}
