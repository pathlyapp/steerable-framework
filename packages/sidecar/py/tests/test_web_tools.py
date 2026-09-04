"""web_search / web_fetch: bounds, SSRF policy, provider seam, approval
gating, and the egress-confined loud failure. All hermetic — HTTP goes
through ``httpx.MockTransport`` and DNS through an injected resolver table;
no test touches the real network.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import httpx
import pytest
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime import (
    ApprovalExecutor,
    InMemorySpillStore,
    RouterToolExecutor,
    SpillHooks,
    ToolRouter,
)
from steerable_agent_runtime.approval import (
    ApprovalDecision,
    ApprovalRequest,
    SessionApprovalCache,
)
from steerable_agent_runtime.loop import LoopContext

from steerable_sidecar.web_tools import (
    TavilySearchProvider,
    WebSearchHit,
    WebToolsConfig,
    _assert_public_address,
    _parse_fetch_url,
    default_web_search_provider,
    register_web_tools,
)


def _make_router(
    *,
    handler: httpx.MockTransport | None = None,
    requests: list[httpx.Request] | None = None,
    dns: dict[str, list[str]] | None = None,
    environ: dict[str, str] | None = None,
    config: WebToolsConfig | None = None,
    search_provider: Any = None,
) -> ToolRouter:
    """Router with the web pair registered against hermetic seams."""
    transport = handler or httpx.MockTransport(lambda request: httpx.Response(404))
    if requests is not None:
        inner = transport.handler

        def recording(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return inner(request)

        transport = httpx.MockTransport(recording)

    def client_factory(timeout: httpx.Timeout) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=timeout)

    async def fake_resolve(host: str, port: int) -> list[str]:
        table = dns if dns is not None else {}
        if host in table:
            return table[host]
        raise OSError(f"fake DNS has no entry for {host!r}")

    register_web_tools(
        router := ToolRouter(),
        config=config or WebToolsConfig(),
        search_provider=search_provider
        if search_provider is not None
        else _FakeSearchProvider(),
        client_factory=client_factory,
        resolve_host=fake_resolve,
        environ=environ if environ is not None else {},
    )
    return router


class _FakeSearchProvider:
    """Provider-seam double: returns a scripted hit list, records calls."""

    def __init__(self, hits: list[WebSearchHit] | None = None) -> None:
        self.hits = hits if hits is not None else [
            WebSearchHit(title=f"r{i}", url=f"https://example.com/{i}", snippet="s")
            for i in range(30)
        ]
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, *, max_results: int) -> list[WebSearchHit]:
        self.calls.append((query, max_results))
        return self.hits


async def _call(router: ToolRouter, name: str, arguments: dict) -> Any:
    return await router.dispatch(
        ToolCall(id="t", name=name, arguments=arguments), consent_granted=True
    )


_PUBLIC_DNS = {"example.com": ["93.184.216.34"]}

# ─── config ────────────────────────────────────────────────────────────────


def test_config_defaults_are_bounded() -> None:
    cfg = WebToolsConfig.resolve({})
    assert cfg.fetch_timeout_ms == 30_000
    assert cfg.fetch_max_bytes == 1_000_000
    assert cfg.fetch_max_redirects == 5
    assert cfg.search_max_results == 8
    assert cfg.search_provider == "tavily"
    assert cfg.search_api_key is None


def test_config_reads_env_overrides() -> None:
    cfg = WebToolsConfig.resolve(
        {
            "STEERABLE_WEB_FETCH_TIMEOUT_MS": "5000",
            "STEERABLE_WEB_FETCH_MAX_BYTES": "4096",
            "STEERABLE_WEB_FETCH_MAX_REDIRECTS": "2",
            "STEERABLE_WEB_SEARCH_MAX_RESULTS": "12",
            "TAVILY_API_KEY": "tvly-test",
        }
    )
    assert cfg.fetch_timeout_ms == 5000
    assert cfg.fetch_max_bytes == 4096
    assert cfg.fetch_max_redirects == 2
    assert cfg.search_max_results == 12
    assert cfg.search_api_key == "tvly-test"


@pytest.mark.parametrize(
    "env",
    [
        {"STEERABLE_WEB_FETCH_TIMEOUT_MS": "soon"},
        {"STEERABLE_WEB_FETCH_TIMEOUT_MS": "0"},
        {"STEERABLE_WEB_FETCH_MAX_BYTES": "12"},  # below the 1 KiB floor
        {"STEERABLE_WEB_FETCH_MAX_REDIRECTS": "-1"},
        {"STEERABLE_WEB_FETCH_MAX_REDIRECTS": "99"},  # above the ceiling
        {"STEERABLE_WEB_SEARCH_MAX_RESULTS": "0"},
        {"STEERABLE_WEB_SEARCH_MAX_RESULTS": "21"},  # above the ceiling
    ],
)
def test_config_rejects_invalid_values(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        WebToolsConfig.resolve(env)


# ─── SSRF policy ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "169.254.169.254",  # cloud metadata endpoint
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fd00::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "64:ff9b::a9fe:a9fe",  # NAT64-wrapped 169.254.169.254
        "64:ff9b::7f00:1",  # NAT64-wrapped 127.0.0.1
    ],
)
def test_non_public_addresses_rejected(address: str) -> None:
    with pytest.raises(ValueError, match="non-public"):
        _assert_public_address(address)


def test_public_addresses_pass() -> None:
    _assert_public_address("93.184.216.34")
    _assert_public_address("2606:2800:220:1:248:1893:25c8:1946")


@pytest.mark.parametrize(
    "url, fragment",
    [
        ("ftp://example.com/x", "unsupported scheme"),
        ("file:///etc/passwd", "unsupported scheme"),
        ("https://user:pass@example.com/", "credentials"),
        ("https://", "no host"),
    ],
)
def test_url_shape_rejected(url: str, fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        _parse_fetch_url(url)


def test_url_length_cap() -> None:
    with pytest.raises(ValueError, match="cap"):
        _parse_fetch_url("https://example.com/" + "a" * 3000)


async def test_literal_private_ip_url_never_touches_dns_or_http() -> None:
    requests: list[httpx.Request] = []
    router = _make_router(requests=requests)
    result = await _call(router, "web_fetch", {"url": "http://169.254.169.254/latest"})
    assert result.success is False
    assert "non-public" in result.error
    assert requests == []


async def test_hostname_resolving_to_private_ip_rejected() -> None:
    requests: list[httpx.Request] = []
    router = _make_router(
        requests=requests, dns={"internal.corp": ["10.1.2.3"]}
    )
    result = await _call(router, "web_fetch", {"url": "https://internal.corp/"})
    assert result.success is False
    assert "non-public" in result.error
    assert requests == []


async def test_unresolvable_host_is_a_loud_error() -> None:
    router = _make_router()
    result = await _call(router, "web_fetch", {"url": "https://nope.invalid/"})
    assert result.success is False
    assert "cannot resolve" in result.error


# ─── web_fetch behavior (MockTransport) ─────────────────────────────────────


def _html_page(body: str) -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": "text/html; charset=utf-8"}, content=body.encode()
    )


async def test_fetch_converts_html_to_text() -> None:
    transport = httpx.MockTransport(
        lambda request: _html_page(
            "<html><head><title>t</title><style>x{}</style></head>"
            "<body><h1>Hello</h1><p>World <a href='https://example.com/more'>more</a>"
            "</p><script>var evil=1;</script></body></html>"
        )
    )
    router = _make_router(handler=transport, dns=_PUBLIC_DNS)
    result = await _call(router, "web_fetch", {"url": "https://example.com/"})
    assert result.success is True
    assert result.data["status"] == 200
    assert result.data["truncated"] is False
    content = result.data["content"]
    assert "Hello" in content and "World" in content
    assert "more (https://example.com/more)" in content
    assert "evil" not in content  # script bodies never reach the model


async def test_fetch_byte_cap_marks_truncated() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"x" * 10_000,
        )
    )
    router = _make_router(
        handler=transport,
        dns=_PUBLIC_DNS,
        config=WebToolsConfig(fetch_max_bytes=4_096),
    )
    result = await _call(router, "web_fetch", {"url": "https://example.com/big"})
    assert result.success is True
    assert result.data["truncated"] is True
    assert result.data["bytes"] == 4_096
    assert len(result.data["content"]) == 4_096


async def test_fetch_timeout_is_a_bounded_error() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow server", request=request)

    router = _make_router(
        handler=httpx.MockTransport(slow),
        dns=_PUBLIC_DNS,
        config=WebToolsConfig(fetch_timeout_ms=1_000),
    )
    result = await _call(router, "web_fetch", {"url": "https://example.com/"})
    assert result.success is False
    assert "timed out after 1000ms" in result.error


async def test_same_origin_redirect_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "/new"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"landed")

    router = _make_router(handler=httpx.MockTransport(handler), dns=_PUBLIC_DNS)
    result = await _call(router, "web_fetch", {"url": "https://example.com/old"})
    assert result.success is True
    assert result.data["url"] == "https://example.com/new"
    assert result.data["content"] == "landed"


async def test_cross_origin_redirect_reported_not_followed() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://other.example/page"})

    router = _make_router(
        handler=httpx.MockTransport(handler),
        requests=requests,
        dns=_PUBLIC_DNS,
    )
    result = await _call(router, "web_fetch", {"url": "https://example.com/start"})
    assert result.success is False
    assert "cross-origin redirect not followed" in result.error
    assert "https://other.example/page" in result.error  # target named for re-issue
    # The redirect target was never requested.
    assert [str(r.url) for r in requests] == ["https://example.com/start"]


async def test_redirect_cap_enforced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        hop = int(request.url.path.strip("/") or "0")
        return httpx.Response(302, headers={"location": f"/{hop + 1}"})

    router = _make_router(
        handler=httpx.MockTransport(handler),
        dns=_PUBLIC_DNS,
        config=WebToolsConfig(fetch_max_redirects=2),
    )
    result = await _call(router, "web_fetch", {"url": "https://example.com/0"})
    assert result.success is False
    assert "redirect cap exceeded" in result.error


async def test_redirect_target_revalidated_against_ssrf() -> None:
    """A public URL redirecting to a private one is refused at the hop."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/admin"})

    router = _make_router(
        handler=httpx.MockTransport(handler),
        # First resolution public; the redirect hop re-resolves to private.
        dns={},
    )
    calls = {"n": 0}

    async def rotating_dns(host: str, port: int) -> list[str]:
        calls["n"] += 1
        return ["93.184.216.34"] if calls["n"] == 1 else ["127.0.0.1"]

    register_web_tools(
        router := ToolRouter(),
        config=WebToolsConfig(),
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
        resolve_host=rotating_dns,
        environ={},
    )
    result = await _call(router, "web_fetch", {"url": "https://example.com/"})
    assert result.success is False
    assert "non-public" in result.error


async def test_binary_content_type_refused() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": "image/png"}, content=b"\x89PNG\r\n"
        )
    )
    router = _make_router(handler=transport, dns=_PUBLIC_DNS)
    result = await _call(router, "web_fetch", {"url": "https://example.com/x.png"})
    assert result.success is False
    assert "unsupported content type" in result.error


# ─── egress-confined loud failure ───────────────────────────────────────────


async def test_egress_confined_fetch_fails_loud_without_http() -> None:
    requests: list[httpx.Request] = []
    router = _make_router(
        requests=requests,
        dns=_PUBLIC_DNS,
        environ={"STEERABLE_EGRESS_CONFINED": "1"},
    )
    result = await _call(router, "web_fetch", {"url": "https://example.com/"})
    assert result.success is False
    assert "egress is confined" in result.error
    assert "STEERABLE_EGRESS_CONFINED=1" in result.error
    assert "Remedies:" in result.error  # actionable, names the way out
    assert requests == []  # no request attempted behind the confining proxy


async def test_egress_confined_search_fails_loud() -> None:
    provider = _FakeSearchProvider()
    router = _make_router(
        search_provider=provider, environ={"STEERABLE_EGRESS_CONFINED": "1"}
    )
    result = await _call(router, "web_search", {"query": "x"})
    assert result.success is False
    assert "egress is confined" in result.error
    assert provider.calls == []


# ─── web_search + provider seam ─────────────────────────────────────────────


async def test_search_result_cap_applies() -> None:
    provider = _FakeSearchProvider()  # 30 scripted hits
    router = _make_router(search_provider=provider)
    result = await _call(router, "web_search", {"query": "q", "max_results": 100})
    assert result.success is True
    assert result.data["result_count"] == 20  # per-call ceiling
    assert len(result.data["results"]) == 20
    assert provider.calls == [("q", 20)]


async def test_search_default_cap_from_config() -> None:
    provider = _FakeSearchProvider()
    router = _make_router(
        search_provider=provider, config=WebToolsConfig(search_max_results=5)
    )
    result = await _call(router, "web_search", {"query": "q"})
    assert result.data["result_count"] == 5
    assert provider.calls == [("q", 5)]


def test_unconfigured_backend_leaves_web_search_unregistered() -> None:
    router = ToolRouter()
    registered = register_web_tools(
        router, config=WebToolsConfig(), environ={}
    )
    assert registered == ["web_fetch"]
    assert router.get("web_search") is None
    assert router.get("web_fetch") is not None


def test_unknown_provider_name_fails_loud() -> None:
    with pytest.raises(ValueError, match="unknown web search provider"):
        register_web_tools(
            ToolRouter(),
            config=WebToolsConfig(search_provider="bogus", search_api_key="k"),
            environ={},
        )


def test_default_provider_factory_resolves_tavily() -> None:
    provider = default_web_search_provider(
        WebToolsConfig(search_api_key="tvly-k")
    )
    assert isinstance(provider, TavilySearchProvider)


async def test_tavily_wire_format_and_response_mapping() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "T",
                        "url": "https://example.com/a",
                        "content": "snippet",
                        "published_date": "2026-01-01",
                    },
                    {"title": "no-url"},  # dropped: no url
                ]
            },
        )

    provider = TavilySearchProvider(
        api_key="tvly-k",
        timeout_ms=5_000,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    hits = await provider.search("hello", max_results=7)
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/search"
    assert request.headers["authorization"] == "Bearer tvly-k"
    import json

    assert json.loads(request.content) == {"query": "hello", "max_results": 7}
    assert hits == [
        WebSearchHit(
            title="T",
            url="https://example.com/a",
            snippet="snippet",
            published_at="2026-01-01",
        )
    ]


async def test_tavily_auth_failure_is_actionable() -> None:
    provider = TavilySearchProvider(
        api_key="bad",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(401, json={"error": "nope"})
            )
        ),
    )
    router = _make_router(search_provider=provider)
    result = await _call(router, "web_search", {"query": "q"})
    assert result.success is False
    assert "STEERABLE_WEB_SEARCH_API_KEY" in result.error


# ─── approval gating ────────────────────────────────────────────────────────


class _RecordingApprover:
    """Denies everything; records the request so the test can inspect what
    the prompt would have shown (mode, category, arguments)."""

    def __init__(self, decision: ApprovalDecision) -> None:
        self.decision = decision
        self.requests: list[ApprovalRequest] = []

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return self.decision


async def test_web_fetch_gated_by_approval_and_url_reaches_prompt() -> None:
    requests: list[httpx.Request] = []
    router = _make_router(requests=requests, dns=_PUBLIC_DNS)
    approver = _RecordingApprover(ApprovalDecision("deny_once", "no"))
    executor = ApprovalExecutor(
        RouterToolExecutor(router),
        approver,
        session=SessionApprovalCache(),
    )
    call = ToolCall(
        id="c1", name="web_fetch", arguments={"url": "https://example.com/"}
    )
    result = await executor.execute(call, LoopContext(chat_id="chat-1"))
    assert result.success is False
    assert result.error == "approval_denied"
    assert requests == []  # denied before any network I/O
    # The prompt carries the URL verbatim and classifies the call as a read.
    assert approver.requests[0].arguments["url"] == "https://example.com/"
    assert approver.requests[0].mode == "read"
    assert approver.requests[0].category == "web_fetch"


async def test_web_search_gated_by_approval() -> None:
    provider = _FakeSearchProvider()
    router = _make_router(search_provider=provider)
    approver = _RecordingApprover(ApprovalDecision("deny_once", "no"))
    executor = ApprovalExecutor(RouterToolExecutor(router), approver)
    result = await executor.execute(
        ToolCall(id="c2", name="web_search", arguments={"query": "q"}),
        LoopContext(),
    )
    assert result.success is False
    assert result.error == "approval_denied"
    assert provider.calls == []
    assert approver.requests[0].category == "web_search"
    assert approver.requests[0].mode == "read"


async def test_approval_allow_executes() -> None:
    provider = _FakeSearchProvider([WebSearchHit(title="t", url="https://x.example")])
    router = _make_router(search_provider=provider)
    approver = _RecordingApprover(ApprovalDecision("allow_once"))
    executor = ApprovalExecutor(RouterToolExecutor(router), approver)
    result = await executor.execute(
        ToolCall(id="c3", name="web_search", arguments={"query": "q"}),
        LoopContext(),
    )
    assert result.success is True
    assert result.data["result_count"] == 1


# ─── spill reuse ────────────────────────────────────────────────────────────


async def test_oversized_fetch_result_routes_through_spill() -> None:
    """The transcript bound is the existing SpillHooks, not a tool-local
    truncation: an over-inline-budget payload externalizes with a locator."""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"y" * 20_000,
        )
    )
    router = _make_router(handler=transport, dns=_PUBLIC_DNS)
    result = await _call(router, "web_fetch", {"url": "https://example.com/"})
    assert result.success is True

    store = InMemorySpillStore()
    hooks = SpillHooks(store, max_inline_bytes=16_000, preview_bytes=2_000)
    spilled = await hooks.post_tool_result(
        result, ToolCall(id="t", name="web_fetch", arguments={}), None
    )
    assert spilled.data["spilled"] is True
    assert store.get(spilled.data["locator"]) is not None
    assert spilled.data["total_bytes"] > 16_000
    assert len(spilled.data["preview"]) <= 2_200


def test_registered_tools_are_direct_read_tier() -> None:
    router = _make_router(dns=_PUBLIC_DNS)
    fetch = router.get("web_fetch")
    search = router.get("web_search")
    assert fetch is not None and search is not None
    for registered in (fetch, search):
        assert registered.exposure == "direct"
        assert registered.mode == "read"
        assert registered.require_consent is False


def test_workspace_router_omits_web_tools_when_declined(tmp_path) -> None:
    """An offline task contract (TB 2.1, via headless ``--no-web-tools``)
    must not be handed a reachable network read."""
    from steerable_sidecar.workspace_tools import workspace_tools_for_cwd

    offered = {
        t["function"]["name"]
        for t in workspace_tools_for_cwd(
            tmp_path, jailed=True, web_tools=False
        ).describe_model()
    }
    assert "web_fetch" not in offered
    assert "web_search" not in offered
    assert "bash" in offered

    with_web = {
        t["function"]["name"]
        for t in workspace_tools_for_cwd(tmp_path, jailed=True).describe_model()
    }
    assert "web_fetch" in with_web
