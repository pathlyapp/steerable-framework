//! `web_fetch` / `web_search` — SSRF policy, domain lists, fetch, search.

use std::collections::HashMap;
use std::future::Future;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use reqwest::Url;
use serde_json::{json, Value};

use crate::types::ToolResult;

pub use crate::web_html::html_to_text;

pub const WEB_FETCH: &str = "web_fetch";
pub const WEB_SEARCH: &str = "web_search";
pub const MAX_URL_LENGTH: usize = 2048;
pub const SEARCH_RESULTS_CEILING: usize = 20;
pub const WEB_FETCH_DESCRIPTION: &str = "Fetch one public web page over http(s) and return its text (HTML is converted to plain text). Private/loopback/link-local targets are refused; cross-origin redirects are reported, not followed — re-issue the call with the reported URL.";
pub const WEB_SEARCH_DESCRIPTION: &str = "Search the public web. Returns titled results with URLs and snippets; follow up with web_fetch on a result URL to read the page.";

pub(crate) const USER_AGENT: &str =
    "steerable-sidecar/0.1 (+https://github.com/deeppath/steerable-framework)";
const FAKE_IP: (Ipv4Addr, u8) = (Ipv4Addr::new(198, 18, 0, 0), 15);

#[derive(Clone, Debug)]
pub struct WebToolsConfig {
    pub fetch_timeout_ms: u64,
    pub fetch_max_bytes: usize,
    pub fetch_max_redirects: u32,
    pub search_timeout_ms: u64,
    pub search_max_results: usize,
    pub search_provider: String,
    pub search_api_key: Option<String>,
    pub search_base_url: String,
    pub allowed_domains: Vec<String>,
    pub blocked_domains: Vec<String>,
    pub session_search_cap: u32,
    pub session_fetch_cap: u32,
}

impl Default for WebToolsConfig {
    fn default() -> Self {
        Self {
            fetch_timeout_ms: 30_000,
            fetch_max_bytes: 1_000_000,
            fetch_max_redirects: 5,
            search_timeout_ms: 30_000,
            search_max_results: 8,
            search_provider: "tavily".into(),
            search_api_key: None,
            search_base_url: "https://api.tavily.com".into(),
            allowed_domains: Vec::new(),
            blocked_domains: Vec::new(),
            session_search_cap: 200,
            session_fetch_cap: 0,
        }
    }
}

fn bounded_int(
    env: &HashMap<String, String>,
    name: &str,
    default: i64,
    minimum: i64,
    ceiling: i64,
) -> Result<i64, String> {
    let Some(raw) = env.get(name).map(|s| s.trim()).filter(|s| !s.is_empty()) else {
        return Ok(default);
    };
    let value: i64 = raw
        .parse()
        .map_err(|_| format!("{name} must be an integer, got {raw:?}"))?;
    if !(minimum..=ceiling).contains(&value) {
        return Err(format!(
            "{name} must be within [{minimum}, {ceiling}], got {value}"
        ));
    }
    Ok(value)
}

fn domain_list(env: &HashMap<String, String>, name: &str) -> Vec<String> {
    env.get(name)
        .map(|raw| {
            raw.split(',')
                .map(|entry| {
                    entry
                        .trim()
                        .to_ascii_lowercase()
                        .trim_start_matches('.')
                        .to_string()
                })
                .filter(|entry| !entry.is_empty())
                .collect()
        })
        .unwrap_or_default()
}

fn default_search_base_url(provider: &str) -> &'static str {
    match provider {
        "brave" => "https://api.search.brave.com",
        "ddg" => "https://html.duckduckgo.com",
        _ => "https://api.tavily.com",
    }
}

impl WebToolsConfig {
    pub fn from_env(env: &HashMap<String, String>) -> Result<Self, String> {
        let api_key = [
            "STEERABLE_WEB_SEARCH_API_KEY",
            "TAVILY_API_KEY",
            "BRAVE_SEARCH_API_KEY",
        ]
        .iter()
        .find_map(|name| {
            env.get(*name)
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
        });
        let provider = env
            .get("STEERABLE_WEB_SEARCH_PROVIDER")
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "tavily".into());
        match provider.as_str() {
            "tavily" | "brave" | "ddg" | "host" => {}
            other => {
                return Err(format!(
                    "unknown web search provider {other:?} (available: 'tavily', 'brave', 'ddg', 'host')"
                ));
            }
        }
        let explicit_base = env
            .get("STEERABLE_WEB_SEARCH_BASE_URL")
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty());
        Ok(Self {
            fetch_timeout_ms: bounded_int(
                env,
                "STEERABLE_WEB_FETCH_TIMEOUT_MS",
                30_000,
                1,
                600_000,
            )? as u64,
            fetch_max_bytes: bounded_int(
                env,
                "STEERABLE_WEB_FETCH_MAX_BYTES",
                1_000_000,
                1_024,
                100_000_000,
            )? as usize,
            fetch_max_redirects: bounded_int(env, "STEERABLE_WEB_FETCH_MAX_REDIRECTS", 5, 0, 20)?
                as u32,
            search_timeout_ms: bounded_int(
                env,
                "STEERABLE_WEB_SEARCH_TIMEOUT_MS",
                30_000,
                1,
                600_000,
            )? as u64,
            search_max_results: bounded_int(env, "STEERABLE_WEB_SEARCH_MAX_RESULTS", 8, 1, 20)?
                as usize,
            search_base_url: explicit_base
                .unwrap_or_else(|| default_search_base_url(&provider).to_string()),
            search_provider: provider,
            search_api_key: api_key,
            allowed_domains: domain_list(env, "STEERABLE_WEB_ALLOWED_DOMAINS"),
            blocked_domains: domain_list(env, "STEERABLE_WEB_BLOCKED_DOMAINS"),
            session_search_cap: bounded_int(
                env,
                "STEERABLE_WEB_SESSION_SEARCH_CAP",
                200,
                0,
                1_000_000,
            )? as u32,
            session_fetch_cap: bounded_int(env, "STEERABLE_WEB_SESSION_FETCH_CAP", 0, 0, 1_000_000)?
                as u32,
        })
    }

    pub fn from_process_env() -> Result<Self, String> {
        Self::from_env(&std::env::vars().collect())
    }

    pub fn search_configured(&self) -> bool {
        match self.search_provider.as_str() {
            "host" | "ddg" => true,
            "brave" | "tavily" => self
                .search_api_key
                .as_deref()
                .is_some_and(|key| !key.is_empty()),
            _ => false,
        }
    }
}

#[derive(Clone, Debug)]
pub struct HttpResponse {
    pub status: u16,
    pub location: Option<String>,
    pub content_type: String,
    pub body: Vec<u8>,
}

impl HttpResponse {
    fn is_redirect(&self) -> bool {
        (300..400).contains(&self.status)
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct WebSearchHit {
    pub title: String,
    pub url: String,
    pub snippet: String,
    pub published_at: Option<String>,
}

fn v4_in(ip: Ipv4Addr, base: Ipv4Addr, prefix: u8) -> bool {
    let mask = if prefix == 0 {
        0
    } else {
        !0u32 << (32 - prefix)
    };
    (u32::from(ip) & mask) == (u32::from(base) & mask)
}

fn is_fake_ip(ip: Ipv4Addr) -> bool {
    v4_in(ip, FAKE_IP.0, FAKE_IP.1)
}

fn ipv4_non_public(ip: Ipv4Addr) -> bool {
    [
        (Ipv4Addr::new(0, 0, 0, 0), 8),
        (Ipv4Addr::new(10, 0, 0, 0), 8),
        (Ipv4Addr::new(100, 64, 0, 0), 10),
        (Ipv4Addr::new(127, 0, 0, 0), 8),
        (Ipv4Addr::new(169, 254, 0, 0), 16),
        (Ipv4Addr::new(172, 16, 0, 0), 12),
        (Ipv4Addr::new(192, 0, 0, 0), 24),
        (Ipv4Addr::new(192, 0, 2, 0), 24),
        (Ipv4Addr::new(192, 168, 0, 0), 16),
        (Ipv4Addr::new(198, 18, 0, 0), 15),
        (Ipv4Addr::new(198, 51, 100, 0), 24),
        (Ipv4Addr::new(203, 0, 113, 0), 24),
        (Ipv4Addr::new(224, 0, 0, 0), 4),
        (Ipv4Addr::new(240, 0, 0, 0), 4),
    ]
    .into_iter()
    .any(|(base, prefix)| v4_in(ip, base, prefix))
}

fn nat64_v4(ip: Ipv6Addr) -> Option<Ipv4Addr> {
    let o = ip.octets();
    if o[0] == 0x00
        && o[1] == 0x64
        && o[2] == 0xff
        && o[3] == 0x9b
        && o[4..12].iter().all(|b| *b == 0)
    {
        Some(Ipv4Addr::new(o[12], o[13], o[14], o[15]))
    } else {
        None
    }
}

fn ipv6_unique_local(ip: Ipv6Addr) -> bool {
    ip.octets()[0] & 0xfe == 0xfc
}

fn ipv6_documentation(ip: Ipv6Addr) -> bool {
    let o = ip.octets();
    o[0] == 0x20 && o[1] == 0x01 && o[2] == 0x0d && o[3] == 0xb8
}

fn ipv6_non_public(ip: Ipv6Addr) -> bool {
    ip.is_unspecified()
        || ip.is_loopback()
        || ip.is_multicast()
        || ip.is_unicast_link_local()
        || ipv6_unique_local(ip)
        || ipv6_documentation(ip)
}

fn candidates(address: IpAddr) -> Vec<IpAddr> {
    let mut out = vec![address];
    if let IpAddr::V6(v6) = address {
        if let Some(mapped) = v6.to_ipv4_mapped() {
            out.push(IpAddr::V4(mapped));
        }
        if let Some(nat64) = nat64_v4(v6) {
            out.push(IpAddr::V4(nat64));
        }
    }
    out
}

pub fn assert_public_address(address: &str) -> Result<(), String> {
    let ip: IpAddr = address
        .parse()
        .map_err(|_| format!("unparseable resolved address: {address:?}"))?;
    for candidate in candidates(ip) {
        let ok = match candidate {
            IpAddr::V4(v4) => !ipv4_non_public(v4) || is_fake_ip(v4),
            IpAddr::V6(v6) => !ipv6_non_public(v6),
        };
        if !ok {
            return Err(format!(
                "refusing to fetch a non-public address ({address}): \
                 loopback, private, link-local (incl. 169.254.169.254-style \
                 metadata endpoints), and reserved ranges are off-limits"
            ));
        }
    }
    Ok(())
}

pub fn parse_fetch_url(url: &str) -> Result<(String, u16), String> {
    if url.len() > MAX_URL_LENGTH {
        return Err(format!(
            "url exceeds the {MAX_URL_LENGTH}-char cap ({} chars)",
            url.len()
        ));
    }
    let parsed = match Url::parse(url) {
        Ok(parsed) => parsed,
        Err(exc) => {
            let message = exc.to_string();
            if message.contains("empty host") || message.contains("host") && url.ends_with("://") {
                return Err("url has no host".into());
            }
            return Err(format!("unparseable url: {exc}"));
        }
    };
    let scheme = parsed.scheme();
    if scheme != "http" && scheme != "https" {
        return Err(format!(
            "unsupported scheme {scheme:?}: web_fetch only fetches http(s)"
        ));
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("credentials in the URL (user:pass@host) are not allowed".into());
    }
    let host = parsed
        .host_str()
        .filter(|host| !host.is_empty())
        .ok_or_else(|| "url has no host".to_string())?;
    let port = parsed
        .port_or_known_default()
        .ok_or_else(|| format!("invalid port in url: {url:?}"))?;
    Ok((host.to_string(), port))
}

fn domain_matches(host: &str, domain: &str) -> bool {
    let host = host.to_ascii_lowercase();
    host == domain || host.ends_with(&format!(".{domain}"))
}

pub fn domain_policy_error(url: &str, config: &WebToolsConfig) -> Option<String> {
    let host = Url::parse(url)
        .ok()
        .and_then(|parsed| parsed.host_str().map(str::to_ascii_lowercase))
        .filter(|host| !host.is_empty())?;
    if config
        .blocked_domains
        .iter()
        .any(|domain| domain_matches(&host, domain))
    {
        return Some(format!(
            "{host} is on this deployment's blocked-domains list (STEERABLE_WEB_BLOCKED_DOMAINS)"
        ));
    }
    if !config.allowed_domains.is_empty()
        && !config
            .allowed_domains
            .iter()
            .any(|domain| domain_matches(&host, domain))
    {
        return Some(format!(
            "{host} is outside this deployment's allowed-domains list (STEERABLE_WEB_ALLOWED_DOMAINS: {})",
            config.allowed_domains.join(", ")
        ));
    }
    None
}

pub async fn validate_fetch_target<F, Fut>(url: &str, resolve: F) -> Result<(), String>
where
    F: FnOnce(&str, u16) -> Fut,
    Fut: Future<Output = Result<Vec<String>, String>>,
{
    let (host, port) = parse_fetch_url(url)?;
    if host.parse::<IpAddr>().is_ok() {
        return assert_public_address(&host);
    }
    let addresses = resolve(&host, port).await?;
    if addresses.is_empty() {
        return Err(format!("cannot resolve {host:?}: no addresses"));
    }
    for address in addresses {
        assert_public_address(&address)?;
    }
    Ok(())
}

fn origin_key(url: &str) -> Option<(String, String, u16)> {
    let parsed = Url::parse(url).ok()?;
    let host = parsed.host_str()?.to_ascii_lowercase();
    let port = parsed.port_or_known_default()?;
    Some((parsed.scheme().to_ascii_lowercase(), host, port))
}

pub fn same_origin(url_a: &str, url_b: &str) -> bool {
    origin_key(url_a) == origin_key(url_b)
}

pub async fn web_fetch<R, FutR, G, FutG>(
    url: &str,
    config: &WebToolsConfig,
    mut resolve: R,
    mut get: G,
) -> ToolResult
where
    R: FnMut(&str, u16) -> FutR,
    FutR: Future<Output = Result<Vec<String>, String>>,
    G: FnMut(&str) -> FutG,
    FutG: Future<Output = Result<HttpResponse, String>>,
{
    let url = url.trim();
    if url.is_empty() {
        return ToolResult::fail("url is empty");
    }
    if let Some(error) = domain_policy_error(url, config) {
        return ToolResult::fail(error);
    }
    let mut current = url.to_string();
    for _ in 0..=config.fetch_max_redirects {
        let host_port = parse_fetch_url(&current);
        if let Err(error) = host_port {
            return ToolResult::fail(error);
        }
        let (host, port) = host_port.expect("parsed");
        if let Err(error) = validate_one(&host, port, &mut resolve).await {
            return ToolResult::fail(error);
        }
        let response = match get(&current).await {
            Ok(response) => response,
            Err(error) if error.contains("timed out") => {
                return ToolResult::fail(format!(
                    "web_fetch timed out after {}ms fetching {current}",
                    config.fetch_timeout_ms
                ));
            }
            Err(error) => {
                return ToolResult::fail(format!(
                    "web_fetch request failed for {current}: {error}"
                ));
            }
        };
        if response.is_redirect() {
            let Some(location) = response.location.as_deref().filter(|s| !s.is_empty()) else {
                return ToolResult::fail(format!(
                    "redirect ({}) without a Location header",
                    response.status
                ));
            };
            let target = match Url::parse(&current).and_then(|base| base.join(location)) {
                Ok(joined) => joined.to_string(),
                Err(_) => location.to_string(),
            };
            if !same_origin(&current, &target) {
                return ToolResult::fail_with_data(
                    format!(
                        "cross-origin redirect not followed: {current} → {target}. \
                         Re-issue web_fetch against the target URL directly if you want it."
                    ),
                    json!({"redirect_to": target}),
                );
            }
            current = target;
            continue;
        }
        return read_response(&response, &current, config);
    }
    ToolResult::fail(format!(
        "redirect cap exceeded: more than {} redirects from {url}",
        config.fetch_max_redirects
    ))
}

async fn validate_one<R, FutR>(host: &str, port: u16, resolve: &mut R) -> Result<(), String>
where
    R: FnMut(&str, u16) -> FutR,
    FutR: Future<Output = Result<Vec<String>, String>>,
{
    if host.parse::<IpAddr>().is_ok() {
        return assert_public_address(host);
    }
    let addresses = match resolve(host, port).await {
        Ok(addresses) => addresses,
        Err(error) => {
            if error.contains("cannot resolve") {
                return Err(error);
            }
            return Err(format!("cannot resolve {host:?}: {error}"));
        }
    };
    if addresses.is_empty() {
        return Err(format!("cannot resolve {host:?}: no addresses"));
    }
    for address in addresses {
        assert_public_address(&address)?;
    }
    Ok(())
}

fn read_response(response: &HttpResponse, url: &str, config: &WebToolsConfig) -> ToolResult {
    let truncated = response.body.len() > config.fetch_max_bytes;
    let raw = if truncated {
        &response.body[..config.fetch_max_bytes]
    } else {
        response.body.as_slice()
    };
    let content_type = response
        .content_type
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    let texty = content_type.starts_with("text/")
        || matches!(
            content_type.as_str(),
            "application/json" | "application/xml" | "application/xhtml+xml" | ""
        );
    if !texty {
        return ToolResult::fail(format!(
            "unsupported content type {content_type:?} at {url}: web_fetch \
             returns text only; download binaries with bash (curl) instead"
        ));
    }
    let decoded = String::from_utf8_lossy(raw);
    let content = if content_type == "text/html" || content_type == "application/xhtml+xml" {
        html_to_text(&decoded)
    } else {
        decoded.into_owned()
    };
    ToolResult::ok(json!({
        "url": url,
        "status": response.status,
        "content_type": if content_type.is_empty() { Value::Null } else { json!(content_type) },
        "bytes": raw.len(),
        "truncated": truncated,
        "content": content,
    }))
}

pub fn clamp_search_results(max_results: Option<u64>, config: &WebToolsConfig) -> usize {
    let requested = max_results.unwrap_or(config.search_max_results as u64) as usize;
    requested.clamp(1, SEARCH_RESULTS_CEILING)
}

pub fn web_search_hits(hits: Vec<WebSearchHit>, config: &WebToolsConfig) -> ToolResult {
    let filtered: Vec<WebSearchHit> = hits
        .into_iter()
        .filter(|hit| domain_policy_error(&hit.url, config).is_none())
        .collect();
    let results: Vec<Value> = filtered
        .iter()
        .map(|hit| {
            let mut row = json!({
                "title": hit.title,
                "url": hit.url,
                "snippet": hit.snippet,
            });
            if let Some(published) = &hit.published_at {
                row["published_at"] = json!(published);
            }
            row
        })
        .collect();
    ToolResult::ok(json!({
        "result_count": results.len(),
        "results": results,
    }))
}

pub fn web_fetch_schema() -> Value {
    json!({
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The http(s) URL to fetch."}
        },
        "required": ["url"],
        "additionalProperties": false
    })
}

pub fn web_search_schema() -> Value {
    json!({
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
            "max_results": {
                "type": "integer",
                "description": "Cap on returned results (default 8, ceiling 20)."
            }
        },
        "required": ["query"],
        "additionalProperties": false
    })
}

pub fn tool_descriptor(name: &str, description: &str, parameters: Value) -> Value {
    json!({
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters
        }
    })
}

pub async fn web_fetch_live(url: &str, config: &WebToolsConfig) -> ToolResult {
    let timeout_ms = config.fetch_timeout_ms;
    web_fetch(
        url,
        config,
        |host, port| {
            let host = host.to_string();
            async move {
                let addrs = tokio::net::lookup_host((host.clone(), port))
                    .await
                    .map_err(|error| format!("cannot resolve {host:?}: {error}"))?;
                let ips: Vec<String> = addrs.map(|addr| addr.ip().to_string()).collect();
                if ips.is_empty() {
                    Err(format!("cannot resolve {host:?}: no addresses"))
                } else {
                    Ok(ips)
                }
            }
        },
        |url| {
            let url = url.to_string();
            async move {
                let client = reqwest::Client::builder()
                    .redirect(reqwest::redirect::Policy::none())
                    .timeout(std::time::Duration::from_millis(timeout_ms))
                    .user_agent(USER_AGENT)
                    .build()
                    .map_err(|error| error.to_string())?;
                let response = client.get(&url).send().await.map_err(|error| {
                    if error.is_timeout() {
                        format!("timed out: {error}")
                    } else {
                        error.to_string()
                    }
                })?;
                let status = response.status().as_u16();
                let location = response
                    .headers()
                    .get(reqwest::header::LOCATION)
                    .and_then(|value| value.to_str().ok())
                    .map(str::to_string);
                let content_type = response
                    .headers()
                    .get(reqwest::header::CONTENT_TYPE)
                    .and_then(|value| value.to_str().ok())
                    .unwrap_or("")
                    .to_string();
                let body = response.bytes().await.map_err(|error| error.to_string())?;
                Ok(HttpResponse {
                    status,
                    location,
                    content_type,
                    body: body.to_vec(),
                })
            }
        },
    )
    .await
}
