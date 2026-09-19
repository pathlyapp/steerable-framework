//! CONNECT forward proxy.

use std::sync::Arc;
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::Mutex;

use crate::allow::AllowList;
use crate::forward::{forward_request, InjectRule};

pub const MAX_HEAD_BYTES: usize = 16 * 1024;

pub struct ProxyConfig {
    pub allow: Arc<Mutex<AllowList>>,
    pub bind_host: String,
    pub bind_port: u16,
    pub connect_timeout: Duration,
    pub inject: Option<InjectRule>,
    pub record_requests: Option<String>,
    pub control_token: Option<String>,
    pub control_port: u16,
}

pub struct EgressProxyServer {
    pub config: ProxyConfig,
    listener: Option<TcpListener>,
    control_listener: Option<TcpListener>,
    control_port: Option<u16>,
}

impl EgressProxyServer {
    pub fn new(config: ProxyConfig) -> Self {
        Self {
            config,
            listener: None,
            control_listener: None,
            control_port: None,
        }
    }

    pub async fn bind(&mut self) -> std::io::Result<u16> {
        let listener =
            TcpListener::bind((self.config.bind_host.as_str(), self.config.bind_port)).await?;
        let port = listener.local_addr()?.port();
        self.listener = Some(listener);
        Ok(port)
    }

    pub async fn bind_control(&mut self) -> std::io::Result<Option<u16>> {
        let Some(_) = self.config.control_token.as_ref() else {
            return Ok(None);
        };
        let listener = TcpListener::bind(("127.0.0.1", self.config.control_port)).await?;
        let port = listener.local_addr()?.port();
        self.control_listener = Some(listener);
        self.control_port = Some(port);
        println!("EGRESS_CONTROL_PORT={port}");
        Ok(Some(port))
    }

    pub fn bound_port(&self) -> u16 {
        self.listener
            .as_ref()
            .and_then(|l| l.local_addr().ok())
            .map(|a| a.port())
            .unwrap_or(self.config.bind_port)
    }

    pub fn bound_control_port(&self) -> Option<u16> {
        self.control_port
    }

    pub async fn serve(&mut self) -> std::io::Result<()> {
        if self.listener.is_none() {
            self.bind().await?;
        }
        if self.config.control_token.is_some() && self.control_listener.is_none() {
            self.bind_control().await?;
        }
        if let (Some(listener), Some(token)) = (
            self.control_listener.take(),
            self.config.control_token.clone(),
        ) {
            let allow = Arc::clone(&self.config.allow);
            tokio::spawn(async move {
                loop {
                    let Ok((stream, _)) = listener.accept().await else {
                        break;
                    };
                    let allow = Arc::clone(&allow);
                    let token = token.clone();
                    tokio::spawn(async move {
                        let _ = handle_control(stream, allow, token).await;
                    });
                }
            });
        }
        let listener = self.listener.take().expect("listener");
        loop {
            let (stream, _) = listener.accept().await?;
            let allow = Arc::clone(&self.config.allow);
            let timeout = self.config.connect_timeout;
            let inject = self.config.inject.clone();
            let record = self.config.record_requests.clone();
            tokio::spawn(async move {
                let _ = handle_client(stream, allow, timeout, inject, record).await;
            });
        }
    }
}

async fn handle_client(
    mut client: TcpStream,
    allow: Arc<Mutex<AllowList>>,
    timeout: Duration,
    inject: Option<InjectRule>,
    record_requests: Option<String>,
) -> std::io::Result<()> {
    let head = match read_head(&mut client).await? {
        Some(head) => head,
        None => {
            reply(&mut client, 431, "Request Header Fields Too Large").await?;
            return Ok(());
        }
    };
    let Some((method, host, port)) = parse_request_line(&head) else {
        reply(&mut client, 400, "Bad Request").await?;
        return Ok(());
    };
    if method != "CONNECT" {
        if let Some(rule) = inject.as_ref() {
            return forward_request(
                &mut client,
                &head,
                rule,
                timeout,
                record_requests.as_deref(),
            )
            .await;
        }
        reply(&mut client, 405, "Method Not Allowed").await?;
        return Ok(());
    }
    let (Some(host), Some(port)) = (host, port) else {
        reply(&mut client, 400, "Bad Request").await?;
        return Ok(());
    };
    if !allow.lock().await.allows(&host, port) {
        reply(
            &mut client,
            403,
            &format!("Forbidden; egress denied for {host}:{port}"),
        )
        .await?;
        return Ok(());
    }
    let upstream = tokio::time::timeout(timeout, TcpStream::connect((host.as_str(), port))).await;
    let Ok(Ok(mut upstream)) = upstream else {
        reply(&mut client, 502, "Bad Gateway").await?;
        return Ok(());
    };
    reply(&mut client, 200, "Connection Established").await?;
    let (mut cr, mut cw) = client.split();
    let (mut ur, mut uw) = upstream.split();
    let up = async {
        let mut buf = vec![0u8; 64 * 1024];
        loop {
            let n = cr.read(&mut buf).await?;
            if n == 0 {
                break;
            }
            uw.write_all(&buf[..n]).await?;
        }
        Ok::<_, std::io::Error>(())
    };
    let down = async {
        let mut buf = vec![0u8; 64 * 1024];
        loop {
            let n = ur.read(&mut buf).await?;
            if n == 0 {
                break;
            }
            cw.write_all(&buf[..n]).await?;
        }
        Ok::<_, std::io::Error>(())
    };
    tokio::select! {
        _ = up => {}
        _ = down => {}
    }
    Ok(())
}

async fn read_head(stream: &mut TcpStream) -> std::io::Result<Option<Vec<u8>>> {
    let mut buf = Vec::new();
    let mut byte = [0u8; 1];
    while buf.len() <= MAX_HEAD_BYTES {
        let n = stream.read(&mut byte).await?;
        if n == 0 {
            break;
        }
        buf.push(byte[0]);
        if buf.windows(4).any(|w| w == b"\r\n\r\n") {
            return Ok(Some(buf));
        }
    }
    if buf.windows(4).any(|w| w == b"\r\n\r\n") {
        Ok(Some(buf))
    } else if buf.len() > MAX_HEAD_BYTES {
        Ok(None)
    } else {
        Ok(Some(buf))
    }
}

fn parse_request_line(head: &[u8]) -> Option<(String, Option<String>, Option<u16>)> {
    let first = head.split(|&b| b == b'\n').next()?;
    let line = first.strip_suffix(&[b'\r']).unwrap_or(first);
    let line = std::str::from_utf8(line).ok()?;
    let mut parts = line.split(' ');
    let method = parts.next()?.to_ascii_uppercase();
    let authority = parts.next()?;
    let version = parts.next()?;
    if !version.starts_with("HTTP/") || parts.next().is_some() {
        return None;
    }
    if !authority.contains(':') {
        return Some((method, None, None));
    }
    let (host, port_s) = authority.rsplit_once(':')?;
    let Ok(port) = port_s.parse::<u16>() else {
        return Some((method, None, None));
    };
    if host.is_empty() || port == 0 {
        return Some((method, None, None));
    }
    Some((method, Some(host.to_ascii_lowercase()), Some(port)))
}

pub(crate) async fn reply(stream: &mut TcpStream, code: u16, reason: &str) -> std::io::Result<()> {
    let msg = format!("HTTP/1.1 {code} {reason}\r\ncontent-length: 0\r\n\r\n");
    stream.write_all(msg.as_bytes()).await
}

async fn reply_json(stream: &mut TcpStream, body: &str) -> std::io::Result<()> {
    let msg = format!(
        "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(msg.as_bytes()).await
}

async fn handle_control(
    mut stream: TcpStream,
    allow: Arc<Mutex<AllowList>>,
    token: String,
) -> std::io::Result<()> {
    let Some(head) = read_head(&mut stream).await? else {
        reply(&mut stream, 431, "Request Header Fields Too Large").await?;
        return Ok(());
    };
    let text = String::from_utf8_lossy(&head);
    let mut lines = text.split("\r\n");
    let Some(request_line) = lines.next() else {
        reply(&mut stream, 400, "Bad Request").await?;
        return Ok(());
    };
    let mut parts = request_line.split(' ');
    let Some(method) = parts.next() else {
        reply(&mut stream, 400, "Bad Request").await?;
        return Ok(());
    };
    let Some(path) = parts.next() else {
        reply(&mut stream, 400, "Bad Request").await?;
        return Ok(());
    };
    let mut headers = std::collections::HashMap::new();
    let mut content_length = 0usize;
    for line in lines {
        if line.is_empty() {
            break;
        }
        if let Some((name, value)) = line.split_once(':') {
            let lname = name.trim().to_ascii_lowercase();
            let value = value.trim().to_string();
            if lname == "content-length" {
                content_length = value.parse().unwrap_or(0);
            }
            headers.insert(lname, value);
        }
    }
    let expected = format!("Bearer {token}");
    if headers.get("authorization").map(String::as_str) != Some(expected.as_str()) {
        reply(&mut stream, 401, "Unauthorized").await?;
        return Ok(());
    }
    if method != "POST" || path != "/allow" {
        reply(&mut stream, 404, "Not Found").await?;
        return Ok(());
    }
    if !(1..=4096).contains(&content_length) {
        reply(&mut stream, 400, "Bad Request").await?;
        return Ok(());
    }
    let mut body = vec![0u8; content_length];
    stream.read_exact(&mut body).await?;
    let host = match serde_json::from_slice::<serde_json::Value>(&body)
        .ok()
        .and_then(|value| value.get("host")?.as_str().map(str::to_string))
    {
        Some(host) => host,
        None => {
            reply(&mut stream, 400, "Bad Request").await?;
            return Ok(());
        }
    };
    let entry = match allow.lock().await.add(&host) {
        Ok(entry) => entry,
        Err(_) => {
            reply(&mut stream, 400, "Bad Request").await?;
            return Ok(());
        }
    };
    let mut ports = entry.ports.clone();
    ports.sort_unstable();
    let ports = ports
        .into_iter()
        .map(|p| p.to_string())
        .collect::<Vec<_>>()
        .join(",");
    let payload = format!(
        "{{\"allowed\": \"{}\", \"ports\": \"{ports}\"}}",
        entry.host
    );
    reply_json(&mut stream, &payload).await
}

pub fn parse_bind(bind: &str) -> Result<(String, u16), String> {
    let Some((host, port_s)) = bind.rsplit_once(':') else {
        return Err(format!("--bind must be host:port, got {bind:?}"));
    };
    if host.is_empty() {
        return Err(format!("--bind must be host:port, got {bind:?}"));
    }
    let port: u16 = port_s
        .parse()
        .map_err(|_| format!("--bind must be host:port, got {bind:?}"))?;
    Ok((host.to_string(), port))
}
