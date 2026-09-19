//! Credential-broker forwarding (Python `steerable_egress_proxy.forward`).

use std::collections::HashSet;
use std::sync::Mutex;
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;

use crate::proxy::reply;

const STRIP_REQUEST_HEADERS: &[&str] = &[
    "connection",
    "keep-alive",
    "proxy-connection",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "expect",
];

static TEE_LOCK: Mutex<()> = Mutex::new(());

#[derive(Clone, Debug)]
pub struct InjectRule {
    pub host: String,
    pub secret: String,
    pub header: String,
    pub scheme: String,
    pub port: Option<u16>,
}

impl InjectRule {
    pub fn new(
        host: impl Into<String>,
        secret: impl Into<String>,
        header: impl Into<String>,
        scheme: impl Into<String>,
        port: Option<u16>,
    ) -> Result<Self, String> {
        let host = host.into();
        let secret = secret.into();
        let header = header.into();
        let scheme = scheme.into();
        if host.is_empty() {
            return Err("InjectRule.host is required".into());
        }
        if secret.is_empty() {
            return Err("InjectRule.secret is required (fail-closed)".into());
        }
        if scheme != "https" && scheme != "http" {
            return Err(format!(
                "InjectRule.scheme must be https|http, got {scheme:?}"
            ));
        }
        Ok(Self {
            host,
            secret,
            header,
            scheme,
            port,
        })
    }

    pub fn upstream_port(&self) -> u16 {
        self.port
            .unwrap_or(if self.scheme == "https" { 443 } else { 80 })
    }
}

#[derive(Debug)]
pub struct ForwardedRequest {
    pub method: String,
    pub path: String,
    pub head: Vec<u8>,
    pub body_remaining: usize,
    pub chunked: bool,
}

pub fn parse_and_rewrite_request(
    head: &[u8],
    rule: &InjectRule,
) -> Result<ForwardedRequest, &'static str> {
    let text = String::from_utf8_lossy(head);
    let mut lines = text.split("\r\n");
    let request_line = lines.next().ok_or("400")?;
    let mut parts = request_line.split(' ');
    let method = parts.next().ok_or("400")?;
    let target = parts.next().ok_or("400")?;
    let version = parts.next().ok_or("400")?;
    if parts.next().is_some() || !version.starts_with("HTTP/") {
        return Err("400");
    }
    let Some((hostname, path)) = split_absolute_http(target) else {
        return Err("403");
    };
    if !hostname.eq_ignore_ascii_case(&rule.host) {
        return Err("403");
    }

    let mut headers: Vec<(String, String)> = Vec::new();
    let mut content_length = 0usize;
    let mut chunked = false;
    let strip: HashSet<&str> = STRIP_REQUEST_HEADERS.iter().copied().collect();
    let inject_header = rule.header.to_ascii_lowercase();
    for line in lines {
        if line.is_empty() {
            break;
        }
        let Some((name, value)) = line.split_once(':') else {
            return Err("400");
        };
        let lname = name.trim().to_ascii_lowercase();
        let value = value.trim();
        if lname == "transfer-encoding" && value.to_ascii_lowercase().contains("chunked") {
            chunked = true;
        }
        if strip.contains(lname.as_str()) || lname == inject_header {
            continue;
        }
        if lname == "host" {
            continue;
        }
        if lname == "content-length" {
            content_length = value.parse().map_err(|_| "400")?;
            continue;
        }
        headers.push((name.trim().to_string(), value.to_string()));
    }

    let mut out = format!("{method} {path} HTTP/1.1\r\nHost: {}\r\n", rule.host);
    for (name, value) in &headers {
        out.push_str(name);
        out.push_str(": ");
        out.push_str(value);
        out.push_str("\r\n");
    }
    if content_length > 0 {
        out.push_str(&format!("Content-Length: {content_length}\r\n"));
    }
    out.push_str(&format!(
        "{}: {}\r\nConnection: close\r\n\r\n",
        rule.header, rule.secret
    ));
    Ok(ForwardedRequest {
        method: method.to_string(),
        path,
        head: out.into_bytes(),
        body_remaining: content_length,
        chunked,
    })
}

fn split_absolute_http(target: &str) -> Option<(String, String)> {
    let rest = target.strip_prefix("http://")?;
    let (authority, path_q) = match rest.split_once('/') {
        Some((authority, rest)) => (authority, format!("/{rest}")),
        None => (rest, "/".to_string()),
    };
    if authority.is_empty() {
        return None;
    }
    let hostname = if authority.starts_with('[') {
        let end = authority.find(']')?;
        authority[1..end].to_string()
    } else {
        authority
            .rsplit_once(':')
            .filter(|(_, port)| port.chars().all(|c| c.is_ascii_digit()) && !port.is_empty())
            .map(|(host, _)| host.to_string())
            .unwrap_or_else(|| authority.to_string())
    };
    if hostname.is_empty() {
        return None;
    }
    Some((hostname, path_q))
}

pub fn tee_forwarded_request(path: &str, parsed: &ForwardedRequest, body: &[u8]) {
    let record = serde_json::json!({
        "method": parsed.method,
        "path": parsed.path,
        "body_bytes": body.len(),
        "body": String::from_utf8_lossy(body),
    });
    let _guard = TEE_LOCK.lock().unwrap();
    if let Some(parent) = std::path::Path::new(path).parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    use std::io::Write;
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
    {
        let _ = writeln!(file, "{record}");
    }
}

pub async fn forward_request(
    client: &mut TcpStream,
    head: &[u8],
    rule: &InjectRule,
    timeout: Duration,
    record_requests: Option<&str>,
) -> std::io::Result<()> {
    let parsed = match parse_and_rewrite_request(head, rule) {
        Ok(parsed) if parsed.chunked => {
            reply(client, 501, "Not Implemented").await?;
            return Ok(());
        }
        Ok(parsed) => parsed,
        Err("403") => {
            reply(client, 403, "Forbidden").await?;
            return Ok(());
        }
        Err("501") => {
            reply(client, 501, "Not Implemented").await?;
            return Ok(());
        }
        Err(_) => {
            reply(client, 400, "Bad Request").await?;
            return Ok(());
        }
    };

    let mut upstream = match dial_upstream(rule, timeout).await {
        Ok(stream) => stream,
        Err(_) => {
            reply(client, 502, "Bad Gateway").await?;
            return Ok(());
        }
    };
    upstream.write_all(&parsed.head).await?;
    let mut remaining = parsed.body_remaining;
    let mut body_parts = Vec::new();
    let mut buf = vec![0u8; 64 * 1024];
    while remaining > 0 {
        let take = remaining.min(buf.len());
        let n = client.read(&mut buf[..take]).await?;
        if n == 0 {
            return Ok(());
        }
        upstream.write_all(&buf[..n]).await?;
        if record_requests.is_some() {
            body_parts.extend_from_slice(&buf[..n]);
        }
        remaining -= n;
    }
    if let Some(path) = record_requests {
        tee_forwarded_request(path, &parsed, &body_parts);
    }
    let _ = tokio::io::copy(&mut upstream, client).await;
    Ok(())
}

async fn dial_upstream(rule: &InjectRule, timeout: Duration) -> std::io::Result<Upstream> {
    let tcp = tokio::time::timeout(
        timeout,
        TcpStream::connect((rule.host.as_str(), rule.upstream_port())),
    )
    .await
    .map_err(|_| std::io::Error::new(std::io::ErrorKind::TimedOut, "dial"))?
    .map_err(|err| std::io::Error::new(std::io::ErrorKind::Other, err))?;
    if rule.scheme == "http" {
        return Ok(Upstream::Plain(tcp));
    }
    let connector = tokio_native_tls::TlsConnector::from(
        native_tls::TlsConnector::new()
            .map_err(|err| std::io::Error::new(std::io::ErrorKind::Other, err))?,
    );
    let tls = connector
        .connect(&rule.host, tcp)
        .await
        .map_err(|err| std::io::Error::new(std::io::ErrorKind::Other, err))?;
    Ok(Upstream::Tls(tls))
}

enum Upstream {
    Plain(TcpStream),
    Tls(tokio_native_tls::TlsStream<TcpStream>),
}

impl tokio::io::AsyncRead for Upstream {
    fn poll_read(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
        buf: &mut tokio::io::ReadBuf<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        match self.get_mut() {
            Upstream::Plain(stream) => std::pin::Pin::new(stream).poll_read(cx, buf),
            Upstream::Tls(stream) => std::pin::Pin::new(stream).poll_read(cx, buf),
        }
    }
}

impl tokio::io::AsyncWrite for Upstream {
    fn poll_write(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
        buf: &[u8],
    ) -> std::task::Poll<std::io::Result<usize>> {
        match self.get_mut() {
            Upstream::Plain(stream) => std::pin::Pin::new(stream).poll_write(cx, buf),
            Upstream::Tls(stream) => std::pin::Pin::new(stream).poll_write(cx, buf),
        }
    }

    fn poll_flush(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        match self.get_mut() {
            Upstream::Plain(stream) => std::pin::Pin::new(stream).poll_flush(cx),
            Upstream::Tls(stream) => std::pin::Pin::new(stream).poll_flush(cx),
        }
    }

    fn poll_shutdown(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        match self.get_mut() {
            Upstream::Plain(stream) => std::pin::Pin::new(stream).poll_shutdown(cx),
            Upstream::Tls(stream) => std::pin::Pin::new(stream).poll_shutdown(cx),
        }
    }
}
