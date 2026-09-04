"""``web_search`` / ``web_fetch`` — the sidecar's network-read tools.

Single implementation for every entry point: headless/ACP get them through
``workspace_tools_for_cwd``; the desktop-spawned sidecar registers them in
``__main__`` and the Electron host delegates execution over the forward
``tool.invoke`` RPC (the host's own tool router stays a thin schema +
forwarding layer). Because there is exactly one implementation, the tool
contract (``tool_contract.py``) does not cover these tools — the contract
exists to keep *two* independent implementations of one capability from
diverging.

Bounds are validated config fields resolved from the environment
(``WebToolsConfig.resolve``), never literals buried in the handlers:

- download byte cap (``fetch_max_bytes``) — bounds what a page can push
  into the process; the transcript-side bound is the existing spill hook
  (``SpillHooks`` externalizes oversized ``data``), not a second
  truncation path here;
- per-request timeouts (``fetch_timeout_ms`` / ``search_timeout_ms``);
- redirect hop cap (``fetch_max_redirects``), same-origin only — a
  cross-origin redirect is reported, not followed, so the model re-issues
  the call against the new origin and the approval prompt names it;
- search result cap (``search_max_results``, ceiling 20).

``web_fetch`` takes a model-supplied URL — an SSRF primitive crossing into
the host's network position. Every hop (initial URL and each redirect
target) is validated: http(s) only, no credentials-in-URL, and the host's
DNS answers must ALL be globally reachable (``ipaddress.is_global``), with
IPv4-mapped and NAT64 (64:ff9b::/96) forms unwrapped before the check.
Residual gap, documented honestly: the resolver check and httpx's own
connect resolve twice, so a hostile authoritative DNS could rotate answers
between them (classic TOCTOU). dsh pins the connection to the validated
address; httpx exposes no lookup hook, so per-hop re-validation plus the
short window is the mitigation here.

Egress confinement: when the desktop runs the framework's per-host egress
proxy (``STEERABLE_EGRESS_PROXY=1``), the sidecar's outbound is confined to
the proxy, which only tunnels the configured LLM provider endpoint. The
desktop marks that posture with ``STEERABLE_EGRESS_CONFINED=1`` in the
sidecar env; both tools then fail loud with an actionable error instead of
hanging behind a proxy that 403/405s them.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urljoin, urlsplit

import httpx
from steerable_agent_protocol.generated import ToolResult
from steerable_agent_runtime import ToolRouter

logger = logging.getLogger(__name__)

__all__ = [
    "HostDelegatedSearchProvider",
    "TavilySearchProvider",
    "WebSearchHit",
    "WebSearchProvider",
    "WebToolsConfig",
    "default_web_search_provider",
    "register_web_tools",
]

#: Security invariants stay fixed (not deployment-tunable): the URL length
#: cap bounds what a model can push into prompts, logs, and the approval UI.
_MAX_URL_LENGTH = 2048
#: Per-call ceilings over the configured defaults — a config value above the
#: ceiling is rejected at resolve time, a per-call argument is clamped to it.
_SEARCH_RESULTS_CEILING = 20
_MAX_REDIRECTS_CEILING = 20
#: NAT64 well-known prefix (RFC 6052): the low 32 bits embed an IPv4 address
#: whose reachability decides, not the wrapper's (IANA marks 64:ff9b::/96
#: globally reachable, so ``is_global`` alone would admit 64:ff9b::a9fe:a9fe
#: — 169.254.169.254 in disguise).
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")

_USER_AGENT = "steerable-sidecar/0.1 (+https://github.com/deeppath/steerable-framework)"

#: Env marker the desktop sets on the sidecar process when the framework's
#: per-host egress proxy holds the host list (W1.3.3). Distinct from the
#: desktop's own opt-in var so a proxy *startup failure* fallback cannot
#: leave the sidecar believing it is confined when it is not.
_EGRESS_CONFINED_ENV = "STEERABLE_EGRESS_CONFINED"


def _egress_confined(environ: Mapping[str, str]) -> bool:
    return (environ.get(_EGRESS_CONFINED_ENV) or "").strip() == "1"


def _confined_error(tool: str, target: str) -> str:
    return (
        f"{tool} is unavailable in this deployment: the sidecar's network "
        f"egress is confined to the per-host egress proxy "
        f"({_EGRESS_CONFINED_ENV}=1), which only tunnels the configured LLM "
        f"provider endpoint — this call cannot reach {target!r}. Remedies: "
        "restart the host without the egress proxy (the Seatbelt port-level "
        "egress list still applies), or extend the proxy's allow-list to "
        "include the target host."
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _bounded_int(
    env: Mapping[str, str], name: str, default: int, *, minimum: int, ceiling: int
) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None
    if not minimum <= value <= ceiling:
        raise ValueError(
            f"{name} must be within [{minimum}, {ceiling}], got {value}"
        )
    return value


@dataclass(frozen=True, slots=True)
class WebToolsConfig:
    """Validated bounds for the web tools (deployment-varying choices).

    Resolve from the process environment with ``resolve``; every field has a
    production default so an untouched deployment gets the bounded behavior.
    """

    fetch_timeout_ms: int = 30_000
    fetch_max_bytes: int = 1_000_000
    fetch_max_redirects: int = 5
    search_timeout_ms: int = 30_000
    search_max_results: int = 8
    search_provider: str = "tavily"
    search_api_key: str | None = None
    search_base_url: str = "https://api.tavily.com"

    @classmethod
    def resolve(cls, environ: Mapping[str, str] | None = None) -> "WebToolsConfig":
        """Build from ``STEERABLE_WEB_*`` env vars; invalid values raise.

        Failing loud at resolve time keeps a typo'd bound from silently
        reverting to a default the operator did not choose.
        """
        env = os.environ if environ is None else environ
        api_key = (
            (env.get("STEERABLE_WEB_SEARCH_API_KEY") or "").strip()
            or (env.get("TAVILY_API_KEY") or "").strip()
            or None
        )
        return cls(
            fetch_timeout_ms=_bounded_int(
                env, "STEERABLE_WEB_FETCH_TIMEOUT_MS", 30_000,
                minimum=1, ceiling=600_000,
            ),
            fetch_max_bytes=_bounded_int(
                env, "STEERABLE_WEB_FETCH_MAX_BYTES", 1_000_000,
                minimum=1_024, ceiling=100_000_000,
            ),
            fetch_max_redirects=_bounded_int(
                env, "STEERABLE_WEB_FETCH_MAX_REDIRECTS", 5,
                minimum=0, ceiling=_MAX_REDIRECTS_CEILING,
            ),
            search_timeout_ms=_bounded_int(
                env, "STEERABLE_WEB_SEARCH_TIMEOUT_MS", 30_000,
                minimum=1, ceiling=600_000,
            ),
            search_max_results=_bounded_int(
                env, "STEERABLE_WEB_SEARCH_MAX_RESULTS", 8,
                minimum=1, ceiling=_SEARCH_RESULTS_CEILING,
            ),
            search_provider=(
                (env.get("STEERABLE_WEB_SEARCH_PROVIDER") or "").strip() or "tavily"
            ),
            search_api_key=api_key,
            search_base_url=(
                (env.get("STEERABLE_WEB_SEARCH_BASE_URL") or "").strip()
                or "https://api.tavily.com"
            ),
        )


# ---------------------------------------------------------------------------
# Search provider seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WebSearchHit:
    """One normalized search result, provider-independent."""

    title: str
    url: str
    snippet: str = ""
    published_at: str | None = None


@runtime_checkable
class WebSearchProvider(Protocol):
    """The search backend seam (same grain as ``LLMProvider``: a protocol +
    a default factory + explicit injection at registration, so the backend
    changes without touching the tool)."""

    async def search(self, query: str, *, max_results: int) -> list[WebSearchHit]:
        """Return up to ``max_results`` hits; raise on backend failure."""
        ...


class TavilySearchProvider:
    """Tavily ``POST {base_url}/search`` — the shipped backend.

    The API key comes from ``STEERABLE_WEB_SEARCH_API_KEY`` /
    ``TAVILY_API_KEY`` (never the brokered LLM key: under credential-broker
    mode the sidecar must not hold the real chat key, so search carries its
    own credential).
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.tavily.com",
        timeout_ms: int = 30_000,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._endpoint = base_url.rstrip("/") + "/search"
        self._timeout = httpx.Timeout(timeout_ms / 1000)
        # Test seam: a MockTransport-backed client keeps the wire-format
        # tests hermetic. Production builds a real client per call.
        self._client = client

    async def search(self, query: str, *, max_results: int) -> list[WebSearchHit]:
        if self._client is not None:
            return await self._request(self._client, query, max_results)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._request(client, query, max_results)

    async def _request(
        self, client: httpx.AsyncClient, query: str, max_results: int
    ) -> list[WebSearchHit]:
        try:
            response = await client.post(
                self._endpoint,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"query": query, "max_results": max_results},
            )
        except httpx.TimeoutException as exc:
            raise WebSearchBackendError(
                f"web search timed out: {exc.__class__.__name__}"
            ) from exc
        except httpx.HTTPError as exc:
            raise WebSearchBackendError(f"web search request failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise WebSearchBackendError(
                f"search provider rejected the credential (HTTP "
                f"{response.status_code}) — check STEERABLE_WEB_SEARCH_API_KEY"
            )
        if response.status_code != 200:
            raise WebSearchBackendError(
                f"search provider returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise WebSearchBackendError(
                "search provider returned a non-JSON response"
            ) from exc
        hits: list[WebSearchHit] = []
        for item in payload.get("results") or []:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            hits.append(
                WebSearchHit(
                    title=str(item.get("title") or ""),
                    url=str(item["url"]),
                    snippet=str(item.get("content") or ""),
                    published_at=(
                        str(item["published_date"])
                        if item.get("published_date")
                        else None
                    ),
                )
            )
        return hits


class WebSearchBackendError(Exception):
    """A configured search backend failed (distinct from "not configured",
    which keeps the tool unregistered)."""


class HostDelegatedSearchProvider:
    """Placeholder so ``web_search`` registers without a sidecar search key.

    Desktop injects ``STEERABLE_WEB_SEARCH_PROVIDER=host`` when the LLM
    vendor has hosted search (OpenAI) and no Tavily key is set. Execution
    happens in the Electron host with the chat credential; this class
    exists so ``tool.list`` advertises the tool. In-process invoke is a
    programming error under ``toolsViaHost``.
    """

    async def search(self, query: str, *, max_results: int) -> list[WebSearchHit]:
        raise WebSearchBackendError(
            "web_search provider 'host' is executed by the Electron parent "
            "with the chat credential; this sidecar process does not hold "
            "that key. Set STEERABLE_WEB_SEARCH_API_KEY for in-process Tavily."
        )


def default_web_search_provider(config: WebToolsConfig) -> WebSearchProvider | None:
    """Resolve the configured search backend; ``None`` when unconfigured.

    ``None`` keeps ``web_search`` unregistered — an unconfigured backend must
    not surface as an available-but-broken tool. An unknown provider name is
    misconfiguration and raises.

    ``host`` registers without a sidecar key so the Electron parent can
    execute hosted search. A Tavily key always wins over ``host``.
    """
    name = config.search_provider
    if name == "host":
        if config.search_api_key:
            return TavilySearchProvider(
                api_key=config.search_api_key,
                base_url=config.search_base_url,
                timeout_ms=config.search_timeout_ms,
            )
        return HostDelegatedSearchProvider()
    if name != "tavily":
        raise ValueError(
            f"unknown web search provider {name!r} (available: 'tavily', 'host')"
        )
    if not config.search_api_key:
        return None
    return TavilySearchProvider(
        api_key=config.search_api_key,
        base_url=config.search_base_url,
        timeout_ms=config.search_timeout_ms,
    )


# ---------------------------------------------------------------------------
# SSRF policy
# ---------------------------------------------------------------------------


class WebFetchPolicyError(ValueError):
    """The URL fails the fetch policy; the message is model-actionable."""


#: DNS resolution seam (tests inject a fake table; production hits the
#: resolver). Returns the host's IP address strings.
ResolveHost = Callable[[str, int], Awaitable[list[str]]]


async def _default_resolve_host(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )
    return sorted({info[4][0] for info in infos})


def _assert_public_address(address: str) -> None:
    """Reject any non-globally-reachable address, unwrapping v4-in-v6 forms."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        raise WebFetchPolicyError(f"unparseable resolved address: {address!r}") from None
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            candidates.append(ip.ipv4_mapped)
        if ip in _NAT64_WELL_KNOWN:
            candidates.append(ipaddress.IPv4Address(int(ip) & 0xFFFF_FFFF))
    for candidate in candidates:
        if not candidate.is_global:
            raise WebFetchPolicyError(
                f"refusing to fetch a non-public address ({address}): "
                "loopback, private, link-local (incl. 169.254.169.254-style "
                "metadata endpoints), and reserved ranges are off-limits"
            )


def _parse_fetch_url(url: str) -> tuple[str, int]:
    """Scheme / credentials / length / host checks; returns (host, port)."""
    if len(url) > _MAX_URL_LENGTH:
        raise WebFetchPolicyError(
            f"url exceeds the {_MAX_URL_LENGTH}-char cap ({len(url)} chars)"
        )
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise WebFetchPolicyError(f"unparseable url: {exc}") from None
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise WebFetchPolicyError(
            f"unsupported scheme {parsed.scheme!r}: web_fetch only fetches http(s)"
        )
    if parsed.username is not None or parsed.password is not None:
        raise WebFetchPolicyError(
            "credentials in the URL (user:pass@host) are not allowed"
        )
    host = parsed.hostname
    if not host:
        raise WebFetchPolicyError("url has no host")
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise WebFetchPolicyError(f"invalid port in url: {url!r}") from None
    return host, port


async def _validate_fetch_target(url: str, resolve_host: ResolveHost) -> None:
    """Full per-hop validation: URL shape, then every DNS answer public."""
    host, port = _parse_fetch_url(url)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # a hostname — resolve below
    else:
        _assert_public_address(host)  # a literal IP — no DNS involved
        return
    try:
        addresses = await resolve_host(host, port)
    except (OSError, asyncio.TimeoutError) as exc:
        raise WebFetchPolicyError(f"cannot resolve {host!r}: {exc}") from None
    if not addresses:
        raise WebFetchPolicyError(f"cannot resolve {host!r}: no addresses")
    for address in addresses:
        _assert_public_address(address)


def _same_origin(url_a: str, url_b: str) -> bool:
    a, b = urlsplit(url_a), urlsplit(url_b)
    return (
        a.scheme.lower(),
        (a.hostname or "").lower(),
        a.port or (443 if a.scheme.lower() == "https" else 80),
    ) == (
        b.scheme.lower(),
        (b.hostname or "").lower(),
        b.port or (443 if b.scheme.lower() == "https" else 80),
    )


# ---------------------------------------------------------------------------
# HTML → text (stdlib-only; the byte cap upstream bounds the input)
# ---------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """Visible-text extraction: skip script/style/noscript/template/head,
    newline at block boundaries, annotate links as ``text (href)``."""

    _SKIP = frozenset({"script", "style", "noscript", "template", "head"})
    _BLOCK = frozenset(
        {
            "p", "div", "br", "li", "ul", "ol", "tr", "table", "section",
            "article", "header", "footer", "main", "aside", "nav",
            "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []
        self._link_href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self._BLOCK:
            self._parts.append("\n")
        elif tag == "a":
            self._link_href = dict(attrs).get("href") or None
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in self._BLOCK:
            self._parts.append("\n")
        elif tag == "a" and self._link_href is not None:
            text = "".join(self._link_text).strip()
            self._parts.append(f"{text} ({self._link_href})" if text else "")
            self._link_href = None
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._link_href is not None:
            self._link_text.append(data)
        self._parts.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self._parts).split("\n"))
        return "\n".join(line for line in lines if line).strip()


def _html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    return extractor.text()


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


#: Client construction seam: tests inject a MockTransport-backed client;
#: production builds a real one per call (stateless, no keepalive to leak).
ClientFactory = Callable[[httpx.Timeout], httpx.AsyncClient]


def _default_client_factory(timeout: httpx.Timeout) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html,text/*,application/json;q=0.9,*/*;q=0.5"},
    )


async def _fetch(
    url: str,
    *,
    config: WebToolsConfig,
    client_factory: ClientFactory,
    resolve_host: ResolveHost,
) -> ToolResult:
    timeout = httpx.Timeout(config.fetch_timeout_ms / 1000)
    current = url
    async with client_factory(timeout) as client:
        for hop in range(config.fetch_max_redirects + 1):
            try:
                await _validate_fetch_target(current, resolve_host)
            except WebFetchPolicyError as exc:
                return ToolResult(success=False, error=str(exc), needsFollowup=True)
            try:
                async with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            return ToolResult(
                                success=False,
                                error=(
                                    f"redirect ({response.status_code}) without a "
                                    "Location header"
                                ),
                                needsFollowup=True,
                            )
                        target = urljoin(current, location)
                        if not _same_origin(current, target):
                            return ToolResult(
                                success=False,
                                error=(
                                    f"cross-origin redirect not followed: {current} "
                                    f"→ {target}. Re-issue web_fetch against the "
                                    "target URL directly if you want it."
                                ),
                                needsFollowup=True,
                                data={"redirect_to": target},
                            )
                        current = target
                        continue
                    return await _read_response(response, current, config)
            except httpx.TimeoutException:
                return ToolResult(
                    success=False,
                    error=(
                        f"web_fetch timed out after {config.fetch_timeout_ms}ms "
                        f"fetching {current}"
                    ),
                    needsFollowup=True,
                )
            except httpx.HTTPError as exc:
                return ToolResult(
                    success=False,
                    error=f"web_fetch request failed for {current}: {exc}",
                    needsFollowup=True,
                )
        return ToolResult(
            success=False,
            error=(
                f"redirect cap exceeded: more than {config.fetch_max_redirects} "
                f"redirects from {url}"
            ),
            needsFollowup=True,
        )


async def _read_response(
    response: httpx.Response, url: str, config: WebToolsConfig
) -> ToolResult:
    """Read the body under the byte cap; convert HTML to text."""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    async for chunk in response.aiter_bytes():
        remaining = config.fetch_max_bytes - total
        if remaining <= 0:
            truncated = True
            break
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            total = config.fetch_max_bytes
            truncated = True
            break
        chunks.append(chunk)
        total += len(chunk)
    raw = b"".join(chunks)
    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    texty = (
        content_type.startswith("text/")
        or content_type
        in ("application/json", "application/xml", "application/xhtml+xml", "")
    )
    if not texty:
        return ToolResult(
            success=False,
            error=(
                f"unsupported content type {content_type!r} at {url}: web_fetch "
                "returns text only; download binaries with bash (curl) instead"
            ),
            needsFollowup=True,
        )
    charset = response.charset_encoding or "utf-8"
    decoded = raw.decode(charset, errors="replace")
    content = _html_to_text(decoded) if content_type in ("text/html", "application/xhtml+xml") else decoded
    return ToolResult(
        success=True,
        data={
            "url": url,
            "status": response.status_code,
            "content_type": content_type or None,
            "bytes": len(raw),
            "truncated": truncated,
            "content": content,
        },
    )


# ---------------------------------------------------------------------------
# Schemas + registration
# ---------------------------------------------------------------------------

_WEB_FETCH_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "The http(s) URL to fetch.",
        },
    },
    "required": ["url"],
    "additionalProperties": False,
}

_WEB_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "The search query."},
        "max_results": {
            "type": "integer",
            "description": "Cap on returned results (default 8, ceiling 20).",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


def register_web_tools(
    router: ToolRouter,
    *,
    config: WebToolsConfig | None = None,
    search_provider: WebSearchProvider | None = None,
    client_factory: ClientFactory | None = None,
    resolve_host: ResolveHost | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Register ``web_fetch`` (always) and ``web_search`` (when a search
    backend is configured) on ``router``. Returns the registered names.

    Both register at the ``direct`` exposure tier in ``read`` mode: they are
    primary capabilities (codex and dsh ship them upfront), side-effect-free
    network reads, and approval gating is the executor wrapper's job on
    interactive paths, not the registry's.

    ``config`` defaults to ``WebToolsConfig.resolve(environ)`` and raises
    ``ValueError`` on invalid values — callers decide whether that is fatal
    (headless: yes, fail at load) or degrades to "web tools absent"
    (``__main__``: logs and continues, so a typo'd optional-feature var
    cannot brick the desktop's chat path).
    """
    env = os.environ if environ is None else environ
    cfg = config or WebToolsConfig.resolve(env)
    make_client = client_factory or _default_client_factory
    resolve = resolve_host or _default_resolve_host

    async def web_fetch(url: str = "") -> ToolResult:
        url = (url or "").strip()
        if not url:
            return ToolResult(success=False, error="url is empty", needsFollowup=True)
        if _egress_confined(env):
            return ToolResult(
                success=False,
                error=_confined_error("web_fetch", url),
                needsFollowup=True,
            )
        return await _fetch(
            url, config=cfg, client_factory=make_client, resolve_host=resolve
        )

    router.register(
        web_fetch,
        name="web_fetch",
        mode="read",
        description=(
            "Fetch one public web page over http(s) and return its text "
            "(HTML is converted to plain text). Private/loopback/link-local "
            "targets are refused; cross-origin redirects are reported, not "
            "followed — re-issue the call with the reported URL."
        ),
        schema=_WEB_FETCH_SCHEMA,
        require_consent=False,
    )
    registered = ["web_fetch"]

    provider = (
        search_provider
        if search_provider is not None
        else default_web_search_provider(cfg)
    )
    if provider is not None:

        async def web_search(query: str = "", max_results: int | None = None) -> ToolResult:
            query = (query or "").strip()
            if not query:
                return ToolResult(
                    success=False, error="query is empty", needsFollowup=True
                )
            if _egress_confined(env):
                return ToolResult(
                    success=False,
                    error=_confined_error("web_search", cfg.search_base_url),
                    needsFollowup=True,
                )
            cap = max(
                1,
                min(
                    int(max_results)
                    if isinstance(max_results, (int, float))
                    else cfg.search_max_results,
                    _SEARCH_RESULTS_CEILING,
                ),
            )
            try:
                hits = await provider.search(query, max_results=cap)
            except WebSearchBackendError as exc:
                return ToolResult(success=False, error=str(exc), needsFollowup=True)
            except Exception as exc:  # provider seam is external code
                return ToolResult(
                    success=False,
                    error=f"web search failed: {exc}",
                    needsFollowup=True,
                )
            hits = hits[:cap]
            return ToolResult(
                success=True,
                data={
                    # result_count is the desktop tool card's summary field
                    # (executed-actions-model.ts reads it out of data).
                    "result_count": len(hits),
                    "query": query,
                    "results": [
                        {
                            "title": hit.title,
                            "url": hit.url,
                            "snippet": hit.snippet,
                            **(
                                {"published_at": hit.published_at}
                                if hit.published_at
                                else {}
                            ),
                        }
                        for hit in hits
                    ],
                },
            )

        router.register(
            web_search,
            name="web_search",
            mode="read",
            description=(
                "Search the public web. Returns titled results with URLs and "
                "snippets; follow up with web_fetch on a result URL to read "
                "the page."
            ),
            schema=_WEB_SEARCH_SCHEMA,
            require_consent=False,
        )
        registered.append("web_search")
    else:
        logger.info(
            "web_search not registered: no search backend configured "
            "(set STEERABLE_WEB_SEARCH_API_KEY / TAVILY_API_KEY, or "
            "STEERABLE_WEB_SEARCH_PROVIDER=host for Electron hosted search)"
        )
    return registered
