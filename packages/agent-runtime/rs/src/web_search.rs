//! Live `web_search` backends: Tavily, Brave, DuckDuckGo lite, and host-delegated.

use std::future::Future;
use std::sync::atomic::{AtomicU32, Ordering};

use serde_json::{json, Map, Value};

use crate::types::ToolResult;
use crate::web::{clamp_search_results, web_search_hits, WebSearchHit, WebToolsConfig, USER_AGENT};

pub const HOST_DELEGATED_ERROR: &str = "web_search provider 'host' is executed by the Electron parent with the chat credential; this sidecar process does not hold that key. Set STEERABLE_WEB_SEARCH_API_KEY for in-process Tavily.";

static SEARCH_CALLS: AtomicU32 = AtomicU32::new(0);

#[derive(Clone, Debug)]
pub struct SearchHttpCall {
    pub method: &'static str,
    pub url: String,
    pub headers: Vec<(String, String)>,
    pub body: Option<Value>,
}

fn session_cap_error(cap: u32) -> Option<ToolResult> {
    if cap == 0 {
        return None;
    }
    let used = SEARCH_CALLS.fetch_add(1, Ordering::Relaxed) + 1;
    if used > cap {
        Some(ToolResult::fail(format!(
            "web_search session cap reached ({cap}). Raise STEERABLE_WEB_SESSION_SEARCH_CAP or start a new session."
        )))
    } else {
        None
    }
}

pub fn parse_tavily_results(payload: &Value) -> Vec<WebSearchHit> {
    payload
        .get("results")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|item| {
            let url = item.get("url")?.as_str()?.to_string();
            if url.is_empty() {
                return None;
            }
            Some(WebSearchHit {
                title: item
                    .get("title")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                url,
                snippet: item
                    .get("content")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                published_at: item
                    .get("published_date")
                    .and_then(Value::as_str)
                    .map(str::to_string),
            })
        })
        .collect()
}

pub fn parse_brave_results(payload: &Value) -> Vec<WebSearchHit> {
    payload
        .pointer("/web/results")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|item| {
            let url = item.get("url")?.as_str()?.to_string();
            if url.is_empty() {
                return None;
            }
            Some(WebSearchHit {
                title: item
                    .get("title")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                url,
                snippet: item
                    .get("description")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                published_at: item.get("age").and_then(Value::as_str).map(str::to_string),
            })
        })
        .collect()
}

fn ddg_unwrap_url(href: &str) -> String {
    let mut raw = href.trim().to_string();
    if raw.starts_with("//") {
        raw = format!("https:{raw}");
    }
    let Ok(url) = reqwest::Url::parse(&raw) else {
        return raw;
    };
    let host = url.host_str().unwrap_or("").to_ascii_lowercase();
    if host.ends_with("duckduckgo.com") && url.path().starts_with("/l/") {
        if let Some((_, value)) = url.query_pairs().find(|(key, _)| key == "uddg") {
            return urlencoding_decode(&value);
        }
    }
    raw
}

fn urlencoding_decode(value: &str) -> String {
    percent_decode(value)
}

fn percent_decode(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Ok(byte) =
                u8::from_str_radix(std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or(""), 16)
            {
                out.push(byte);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn class_tokens(tag: &str) -> Vec<String> {
    let Some(start) = tag.find("class=") else {
        return Vec::new();
    };
    let rest = &tag[start + 6..];
    let quote = rest.chars().next();
    let body = match quote {
        Some('"') | Some('\'') => {
            let q = quote.unwrap();
            rest[1..].split(q).next().unwrap_or("")
        }
        _ => rest.split_whitespace().next().unwrap_or(""),
    };
    body.split_whitespace().map(str::to_string).collect()
}

fn tag_attr<'a>(tag: &'a str, name: &str) -> Option<&'a str> {
    let needle = format!("{name}=");
    let start = tag.find(&needle)?;
    let rest = &tag[start + needle.len()..];
    let quote = rest.chars().next()?;
    if quote != '"' && quote != '\'' {
        return None;
    }
    rest[1..].split(quote).next()
}

pub fn parse_ddg_lite_html(html: &str) -> Result<Vec<WebSearchHit>, String> {
    if html.to_ascii_lowercase().contains("anomaly-modal") {
        return Err("DuckDuckGo presented a bot check; try again later or use a Tavily key".into());
    }
    let mut hits = Vec::new();
    let lower = html;
    let mut remaining = lower;
    while let Some(idx) = remaining.find("<a ") {
        let after = &remaining[idx..];
        let end = after.find('>').unwrap_or(after.len());
        let open = &after[..end];
        let tokens = class_tokens(open);
        remaining = &after[end.min(after.len())..];
        if !(tokens
            .iter()
            .any(|t| t == "result__a" || t == "result-link"))
        {
            continue;
        }
        let href = tag_attr(open, "href").unwrap_or("");
        let url = ddg_unwrap_url(href);
        let (title, after_title) = if let Some(close) = remaining.find("</a>") {
            (remaining[..close].to_string(), &remaining[close + 4..])
        } else {
            (String::new(), remaining)
        };
        remaining = after_title;
        let snippet = extract_ddg_snippet(remaining);
        if !url.is_empty() {
            hits.push(WebSearchHit {
                title: strip_tags(&title).trim().to_string(),
                url,
                snippet,
                published_at: None,
            });
        }
    }
    Ok(hits)
}

fn extract_ddg_snippet(html: &str) -> String {
    for class in ["result__snippet", "result-snippet"] {
        let needle = format!("class=\"{class}\"");
        let alt = format!("class='{class}'");
        let pos = html.find(&needle).or_else(|| html.find(&alt));
        if let Some(idx) = pos {
            let after = &html[idx..];
            if let Some(gt) = after.find('>') {
                let body = &after[gt + 1..];
                if let Some(end) = body.find('<') {
                    return strip_tags(&body[..end]).trim().to_string();
                }
            }
        }
    }
    String::new()
}

fn strip_tags(input: &str) -> String {
    let mut out = String::new();
    let mut in_tag = false;
    for ch in input.chars() {
        match ch {
            '<' => in_tag = true,
            '>' => in_tag = false,
            _ if !in_tag => out.push(ch),
            _ => {}
        }
    }
    out
}

fn provider_http_error(status: u16, body: &str) -> ToolResult {
    if status == 401 || status == 403 {
        return ToolResult::fail(format!(
            "search provider rejected the credential (HTTP {status}) — check STEERABLE_WEB_SEARCH_API_KEY"
        ));
    }
    if status == 429 {
        return ToolResult::fail("search provider rate-limited the request (HTTP 429)");
    }
    ToolResult::fail(format!(
        "search provider returned HTTP {status}: {}",
        body.chars().take(200).collect::<String>()
    ))
}

fn tavily_call(query: &str, cap: usize, config: &WebToolsConfig) -> SearchHttpCall {
    let mut body = Map::new();
    body.insert("query".into(), json!(query));
    body.insert("max_results".into(), json!(cap));
    if !config.allowed_domains.is_empty() {
        body.insert("include_domains".into(), json!(config.allowed_domains));
    }
    if !config.blocked_domains.is_empty() {
        body.insert("exclude_domains".into(), json!(config.blocked_domains));
    }
    let key = config.search_api_key.clone().unwrap_or_default();
    SearchHttpCall {
        method: "POST",
        url: format!("{}/search", config.search_base_url.trim_end_matches('/')),
        headers: vec![
            ("Authorization".into(), format!("Bearer {key}")),
            ("User-Agent".into(), USER_AGENT.into()),
        ],
        body: Some(Value::Object(body)),
    }
}

fn brave_call(query: &str, cap: usize, config: &WebToolsConfig) -> SearchHttpCall {
    let mut url = reqwest::Url::parse(&format!(
        "{}/res/v1/web/search",
        config.search_base_url.trim_end_matches('/')
    ))
    .unwrap_or_else(|_| {
        reqwest::Url::parse("https://api.search.brave.com/res/v1/web/search").expect("static")
    });
    url.query_pairs_mut()
        .append_pair("q", query)
        .append_pair("count", &cap.min(20).to_string());
    let key = config.search_api_key.clone().unwrap_or_default();
    SearchHttpCall {
        method: "GET",
        url: url.to_string(),
        headers: vec![
            ("Accept".into(), "application/json".into()),
            ("X-Subscription-Token".into(), key),
            ("User-Agent".into(), USER_AGENT.into()),
        ],
        body: None,
    }
}

fn ddg_call(query: &str, config: &WebToolsConfig) -> SearchHttpCall {
    let mut url = reqwest::Url::parse(&format!(
        "{}/html/",
        config.search_base_url.trim_end_matches('/')
    ))
    .unwrap_or_else(|_| reqwest::Url::parse("https://html.duckduckgo.com/html/").expect("static"));
    url.query_pairs_mut()
        .append_pair("q", query)
        .append_pair("kl", "wt-wt");
    SearchHttpCall {
        method: "GET",
        url: url.to_string(),
        headers: vec![
            ("User-Agent".into(), USER_AGENT.into()),
            ("Accept".into(), "text/html".into()),
        ],
        body: None,
    }
}

pub async fn web_search<F, Fut>(
    query: &str,
    max_results: Option<u64>,
    config: &WebToolsConfig,
    http: F,
) -> ToolResult
where
    F: FnOnce(SearchHttpCall) -> Fut,
    Fut: Future<Output = Result<(u16, String), String>>,
{
    let query = query.trim();
    if query.is_empty() {
        return ToolResult::fail("query is empty");
    }
    if let Some(error) = session_cap_error(config.session_search_cap) {
        return error;
    }
    if config.search_provider == "host" && config.search_api_key.is_none() {
        return ToolResult::fail(HOST_DELEGATED_ERROR);
    }
    let cap = clamp_search_results(max_results, config);
    let call = match config.search_provider.as_str() {
        "brave" => brave_call(query, cap, config),
        "ddg" => ddg_call(query, config),
        _ => tavily_call(query, cap, config),
    };
    let (status, body) = match http(call).await {
        Ok(pair) => pair,
        Err(error) if error.contains("timed out") => {
            return ToolResult::fail(format!("web search timed out: {error}"));
        }
        Err(error) => return ToolResult::fail(format!("web search request failed: {error}")),
    };
    if status == 202 && config.search_provider == "ddg" {
        return ToolResult::fail(
            "DuckDuckGo presented a bot check; try again later or use a Tavily key",
        );
    }
    if status == 401 || status == 403 {
        if config.search_provider == "ddg" {
            return ToolResult::fail(format!(
                "DuckDuckGo refused the request (HTTP {status}); try again later or use a Tavily key"
            ));
        }
        return provider_http_error(status, &body);
    }
    if status != 200 {
        return provider_http_error(status, &body);
    }
    let hits = match config.search_provider.as_str() {
        "brave" => {
            let payload: Value = match serde_json::from_str(&body) {
                Ok(value) => value,
                Err(_) => return ToolResult::fail("search provider returned a non-JSON response"),
            };
            parse_brave_results(&payload)
        }
        "ddg" => match parse_ddg_lite_html(&body) {
            Ok(hits) => hits,
            Err(error) => return ToolResult::fail(error),
        },
        _ => {
            let payload: Value = match serde_json::from_str(&body) {
                Ok(value) => value,
                Err(_) => return ToolResult::fail("search provider returned a non-JSON response"),
            };
            parse_tavily_results(&payload)
        }
    };
    let mut result = web_search_hits(hits.into_iter().take(cap).collect(), config);
    if let Some(data) = result.data.as_mut() {
        if let Some(object) = data.as_object_mut() {
            object.insert("query".into(), json!(query));
        }
    }
    result
}

pub async fn web_search_live(
    query: &str,
    max_results: Option<u64>,
    config: &WebToolsConfig,
) -> ToolResult {
    let timeout_ms = config.search_timeout_ms;
    web_search(query, max_results, config, |call| async move {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_millis(timeout_ms))
            .user_agent(USER_AGENT)
            .build()
            .map_err(|error| error.to_string())?;
        let mut request = match call.method {
            "POST" => client
                .post(&call.url)
                .json(&call.body.clone().unwrap_or(json!({}))),
            _ => client.get(&call.url),
        };
        for (name, value) in &call.headers {
            request = request.header(name.as_str(), value.as_str());
        }
        let response = request.send().await.map_err(|error| {
            if error.is_timeout() {
                format!("timed out: {error}")
            } else {
                error.to_string()
            }
        })?;
        let status = response.status().as_u16();
        let body = response.text().await.map_err(|error| error.to_string())?;
        Ok((status, body))
    })
    .await
}
