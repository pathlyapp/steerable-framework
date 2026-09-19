use std::collections::HashMap;
use std::future::Future;

use pretty_assertions::assert_eq;
use steerable_agent_runtime::{
    html_to_text, parse_fetch_url, web_fetch, web_search_hits, HttpResponse, WebSearchHit,
    WebToolsConfig,
};

fn reject_dns(_host: &str, _port: u16) -> impl Future<Output = Result<Vec<String>, String>> {
    async { Err("dns should not run".into()) }
}

fn public_dns(
    table: HashMap<String, Vec<String>>,
) -> impl Fn(&str, u16) -> std::pin::Pin<Box<dyn Future<Output = Result<Vec<String>, String>> + Send>>
{
    move |host, _port| {
        let host = host.to_string();
        let table = table.clone();
        Box::pin(async move {
            table
                .get(&host)
                .cloned()
                .ok_or_else(|| format!("cannot resolve {host:?}: fake DNS has no entry"))
        })
    }
}

fn responses(
    map: HashMap<String, HttpResponse>,
) -> impl Fn(&str) -> std::pin::Pin<Box<dyn Future<Output = Result<HttpResponse, String>> + Send>> {
    move |url| {
        let url = url.to_string();
        let map = map.clone();
        Box::pin(async move {
            map.get(&url)
                .cloned()
                .ok_or_else(|| format!("no mock for {url}"))
        })
    }
}

#[test]
fn url_shape_rejected() {
    for (url, fragment) in [
        ("ftp://example.com/x", "unsupported scheme"),
        ("file:///etc/passwd", "unsupported scheme"),
        ("https://user:pass@example.com/", "credentials"),
        ("https://", "no host"),
    ] {
        let error = parse_fetch_url(url).unwrap_err();
        assert!(error.contains(fragment), "{url}: {error}");
    }
}

#[test]
fn url_length_cap() {
    let url = format!("https://example.com/{}", "a".repeat(3000));
    let error = parse_fetch_url(&url).unwrap_err();
    assert!(error.contains("cap"));
}

#[tokio::test]
async fn literal_private_ip_url_never_touches_dns_or_http() {
    let result = web_fetch(
        "http://169.254.169.254/latest",
        &WebToolsConfig::default(),
        reject_dns,
        |_url| async { Err("http".into()) },
    )
    .await;
    assert!(!result.success);
    assert!(result.error.as_deref().unwrap().contains("non-public"));
}

#[tokio::test]
async fn hostname_resolving_to_private_ip_rejected() {
    let mut table = HashMap::new();
    table.insert("internal.corp".into(), vec!["10.1.2.3".into()]);
    let result = web_fetch(
        "https://internal.corp/",
        &WebToolsConfig::default(),
        public_dns(table),
        |_url| async { Err("http".into()) },
    )
    .await;
    assert!(!result.success);
    assert!(result.error.as_deref().unwrap().contains("non-public"));
}

#[tokio::test]
async fn hostname_resolving_to_fake_ip_is_fetched() {
    let mut table = HashMap::new();
    table.insert("proxied.example".into(), vec!["198.18.0.23".into()]);
    let mut pages = HashMap::new();
    pages.insert(
        "https://proxied.example/".into(),
        HttpResponse {
            status: 200,
            location: None,
            content_type: "text/html; charset=utf-8".into(),
            body: b"<p>ok</p>".to_vec(),
        },
    );
    let result = web_fetch(
        "https://proxied.example/",
        &WebToolsConfig::default(),
        public_dns(table),
        responses(pages),
    )
    .await;
    assert!(result.success, "{:?}", result.error);
}

#[tokio::test]
async fn unresolvable_host_is_a_loud_error() {
    let result = web_fetch(
        "https://nope.invalid/",
        &WebToolsConfig::default(),
        public_dns(HashMap::new()),
        |_url| async { Err("http".into()) },
    )
    .await;
    assert!(!result.success);
    assert!(result.error.as_deref().unwrap().contains("cannot resolve"));
}

#[tokio::test]
async fn fetch_converts_html_to_text() {
    let html = "<html><head><title>t</title><style>x{}</style></head>\
                <body><h1>Hello</h1><p>World <a href='https://example.com/more'>more</a>\
                </p><script>var evil=1;</script></body></html>";
    let mut table = HashMap::new();
    table.insert("example.com".into(), vec!["93.184.216.34".into()]);
    let mut pages = HashMap::new();
    pages.insert(
        "https://example.com/".into(),
        HttpResponse {
            status: 200,
            location: None,
            content_type: "text/html; charset=utf-8".into(),
            body: html.as_bytes().to_vec(),
        },
    );
    let result = web_fetch(
        "https://example.com/",
        &WebToolsConfig::default(),
        public_dns(table),
        responses(pages),
    )
    .await;
    assert!(result.success, "{:?}", result.error);
    let data = result.data.unwrap();
    let content = data["content"].as_str().unwrap();
    assert!(content.contains("Hello") && content.contains("World"));
    assert!(content.contains("more (https://example.com/more)"));
    assert!(!content.contains("evil"));
}

#[tokio::test]
async fn fetch_byte_cap_marks_truncated() {
    let mut table = HashMap::new();
    table.insert("example.com".into(), vec!["93.184.216.34".into()]);
    let mut pages = HashMap::new();
    pages.insert(
        "https://example.com/big".into(),
        HttpResponse {
            status: 200,
            location: None,
            content_type: "text/plain".into(),
            body: vec![b'x'; 10_000],
        },
    );
    let result = web_fetch(
        "https://example.com/big",
        &WebToolsConfig {
            fetch_max_bytes: 4_096,
            ..WebToolsConfig::default()
        },
        public_dns(table),
        responses(pages),
    )
    .await;
    assert!(result.success);
    let data = result.data.unwrap();
    assert_eq!(data["truncated"], true);
    assert_eq!(data["bytes"], 4096);
}

#[tokio::test]
async fn same_origin_redirect_followed() {
    let mut table = HashMap::new();
    table.insert("example.com".into(), vec!["93.184.216.34".into()]);
    let mut pages = HashMap::new();
    pages.insert(
        "https://example.com/old".into(),
        HttpResponse {
            status: 302,
            location: Some("/new".into()),
            content_type: String::new(),
            body: Vec::new(),
        },
    );
    pages.insert(
        "https://example.com/new".into(),
        HttpResponse {
            status: 200,
            location: None,
            content_type: "text/plain".into(),
            body: b"landed".to_vec(),
        },
    );
    let result = web_fetch(
        "https://example.com/old",
        &WebToolsConfig::default(),
        public_dns(table),
        responses(pages),
    )
    .await;
    assert!(result.success, "{:?}", result.error);
    let data = result.data.unwrap();
    assert_eq!(data["url"], "https://example.com/new");
    assert_eq!(data["content"], "landed");
}

#[tokio::test]
async fn cross_origin_redirect_reported_not_followed() {
    let mut table = HashMap::new();
    table.insert("example.com".into(), vec!["93.184.216.34".into()]);
    let mut pages = HashMap::new();
    pages.insert(
        "https://example.com/start".into(),
        HttpResponse {
            status: 302,
            location: Some("https://other.example/page".into()),
            content_type: String::new(),
            body: Vec::new(),
        },
    );
    let result = web_fetch(
        "https://example.com/start",
        &WebToolsConfig::default(),
        public_dns(table),
        responses(pages),
    )
    .await;
    assert!(!result.success);
    assert!(result
        .error
        .as_deref()
        .unwrap()
        .contains("cross-origin redirect"));
    assert_eq!(
        result.data.unwrap()["redirect_to"],
        "https://other.example/page"
    );
}

#[test]
fn html_skips_script_and_annotates_links() {
    let text = html_to_text(
        "<html><head><title>t</title></head><body><p>Hi <a href='https://x'>go</a></p><script>evil</script></body>",
    );
    assert!(text.contains("Hi"));
    assert!(text.contains("go (https://x)"));
    assert!(!text.contains("evil"));
}

#[test]
fn search_filters_hits_by_domain_policy() {
    let config = WebToolsConfig {
        allowed_domains: vec!["docs.example.com".into()],
        ..WebToolsConfig::default()
    };
    let result = web_search_hits(
        vec![
            WebSearchHit {
                title: "ok".into(),
                url: "https://docs.example.com/a".into(),
                snippet: "a".into(),
                published_at: None,
            },
            WebSearchHit {
                title: "no".into(),
                url: "https://evil.example.com/a".into(),
                snippet: "b".into(),
                published_at: None,
            },
        ],
        &config,
    );
    assert!(result.success);
    assert_eq!(result.data.unwrap()["result_count"], 1);
}

#[test]
fn search_result_cap_applies() {
    assert_eq!(
        steerable_agent_runtime::clamp_search_results(Some(100), &WebToolsConfig::default()),
        20
    );
    assert_eq!(
        steerable_agent_runtime::clamp_search_results(
            None,
            &WebToolsConfig {
                search_max_results: 5,
                ..WebToolsConfig::default()
            }
        ),
        5
    );
}

#[test]
fn from_env_requires_key_for_tavily_and_registers_host() {
    let mut env = HashMap::new();
    env.insert("STEERABLE_WEB_SEARCH_PROVIDER".into(), "tavily".into());
    let tavily = WebToolsConfig::from_env(&env).unwrap();
    assert!(!tavily.search_configured());
    env.insert("STEERABLE_WEB_SEARCH_API_KEY".into(), "tvly-test".into());
    let tavily = WebToolsConfig::from_env(&env).unwrap();
    assert!(tavily.search_configured());
    env.insert("STEERABLE_WEB_SEARCH_PROVIDER".into(), "host".into());
    env.remove("STEERABLE_WEB_SEARCH_API_KEY");
    let host = WebToolsConfig::from_env(&env).unwrap();
    assert!(host.search_configured());
}

#[tokio::test]
async fn tavily_search_posts_and_normalizes_hits() {
    use steerable_agent_runtime::{web_search, SearchHttpCall};
    let config = WebToolsConfig {
        search_provider: "tavily".into(),
        search_api_key: Some("tvly-test".into()),
        search_base_url: "https://api.tavily.com".into(),
        session_search_cap: 0,
        ..WebToolsConfig::default()
    };
    let result = web_search(
        "steerable",
        Some(3),
        &config,
        |call: SearchHttpCall| async move {
            assert_eq!(call.method, "POST");
            assert!(call.url.ends_with("/search"));
            assert!(call
                .headers
                .iter()
                .any(|(name, value)| name == "Authorization" && value == "Bearer tvly-test"));
            Ok((
                200,
                serde_json::json!({
                    "results": [
                        {"title": "A", "url": "https://docs.example.com/a", "content": "one"},
                        {"title": "B", "url": "https://docs.example.com/b", "content": "two"}
                    ]
                })
                .to_string(),
            ))
        },
    )
    .await;
    assert!(result.success);
    let data = result.data.unwrap();
    assert_eq!(data["result_count"], 2);
    assert_eq!(data["query"], "steerable");
}

#[tokio::test]
async fn host_provider_without_key_fails_with_delegation_message() {
    use steerable_agent_runtime::{web_search, HOST_DELEGATED_ERROR};
    let config = WebToolsConfig {
        search_provider: "host".into(),
        search_api_key: None,
        session_search_cap: 0,
        ..WebToolsConfig::default()
    };
    let result = web_search("q", None, &config, |_call| async { Ok((200, "{}".into())) }).await;
    assert!(!result.success);
    assert_eq!(result.error.as_deref(), Some(HOST_DELEGATED_ERROR));
}

#[test]
fn ddg_lite_parser_unwraps_redirects() {
    let html = r#"<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage">Example</a><a class="result__snippet">Hello there</a>"#;
    let hits = steerable_agent_runtime::parse_ddg_lite_html(html).unwrap();
    assert_eq!(hits.len(), 1);
    assert_eq!(hits[0].url, "https://example.com/page");
    assert_eq!(hits[0].title, "Example");
}
