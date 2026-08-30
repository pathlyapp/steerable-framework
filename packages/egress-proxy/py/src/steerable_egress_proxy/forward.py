"""Credential-broker forwarding: plain-HTTP in, authenticated HTTPS out.

This module is the W2.2.2 half of the egress proxy. The sandboxed sidecar
points its LLM `baseUrl` at the *http* scheme of the provider host and routes
through this proxy; the proxy terminates the plain-HTTP request, dials the
real upstream over TLS, and injects the credential header. The secret value
lives only in this process — the sidecar (and anything it spawns) never sees
it, which is the entire point of the broker pattern (codex network-proxy
takes the same route).

Fail-closed rules:

- no ``InjectRule`` configured      → non-CONNECT methods stay 405 (v1 behavior)
- absolute-URI host ≠ rule host     → 403
- client sent its own credential    → stripped, never forwarded
- chunked *request* bodies          → 501 (the OpenAI/Anthropic SDKs send
  Content-Length; anything else is out of v1 scope and must fail loud)

One request per client connection (``Connection: close`` both ways): LLM
streaming connections are long-lived anyway, and dropping keep-alive removes
a whole class of request-smuggling surface.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Headers never forwarded to the upstream: hop-by-hop per RFC 9110 §7.6.1,
#: plus any client-supplied credential (the whole reason the broker exists).
_STRIP_REQUEST_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-connection",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "expect",
    }
)


@dataclass(frozen=True, slots=True)
class InjectRule:
    """One credential-injection target.

    ``secret`` is the full header value (e.g. ``"Bearer sk-..."``); the CLI
    resolves it from an env var so the secret never appears in argv. v1
    supports exactly one rule per proxy — one provider per sidecar is the
    deployment reality, and a single rule keeps the fail-closed surface
    trivially auditable.
    """

    host: str
    secret: str
    header: str = "Authorization"
    #: Upstream scheme. ``http`` exists for tests and loopback-only upstreams;
    #: production deployments must keep the default ``https``.
    scheme: str = "https"
    port: int | None = None  # default: 443 for https, 80 for http

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("InjectRule.host is required")
        if not self.secret:
            raise ValueError("InjectRule.secret is required (fail-closed)")
        if self.scheme not in ("https", "http"):
            raise ValueError(f"InjectRule.scheme must be https|http, got {self.scheme!r}")

    @property
    def upstream_port(self) -> int:
        if self.port is not None:
            return self.port
        return 443 if self.scheme == "https" else 80


@dataclass(frozen=True, slots=True)
class ForwardedRequest:
    """Parsed + rewritten request ready to send to the upstream."""

    method: str
    path: str  # origin-form (path + query)
    head: bytes  # complete rewritten head, CRLF-terminated with blank line
    body_remaining: int  # Content-Length; 0 = no body
    chunked: bool  # True → caller rejects with 501 (v1)


def parse_and_rewrite_request(
    head: bytes, rule: InjectRule
) -> ForwardedRequest | str:
    """Parse the client head and build the upstream request.

    Returns the rewritten request, or a string rejection reason ("403"/"400"/
    "501") — the caller maps it to the status line. Kept pure (no I/O) so the
    rewrite rules are unit-testable without sockets.
    """
    try:
        text = head.decode("iso-8859-1")
    except UnicodeDecodeError:
        return "400"
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        return "400"
    method, target, _version = parts
    url = urlsplit(target)
    if url.scheme != "http" or not url.hostname:
        # Origin-form or non-http absolute URI: not a forward-proxy request
        # this rule can serve.
        return "403"
    if url.hostname.lower() != rule.host.lower():
        logger.info("deny forward %s (not the inject host)", url.hostname)
        return "403"

    headers: list[tuple[str, str]] = []
    content_length = 0
    chunked = False
    for line in lines[1:]:
        if not line:
            break
        name, sep, value = line.partition(":")
        if not sep:
            return "400"
        lname = name.strip().lower()
        value = value.strip()
        if lname in _STRIP_REQUEST_HEADERS or lname == rule.header.lower():
            continue  # hop-by-hop, Expect, and any client credential
        if lname == "host":
            continue  # rewritten below
        if lname == "content-length":
            try:
                content_length = int(value)
            except ValueError:
                return "400"
            if content_length < 0:
                return "400"
            continue  # re-emitted canonically below
        headers.append((name.strip(), value))
    # Transfer-Encoding was stripped above; detect it from the raw lines so a
    # chunked *request* fails loud instead of desyncing the stream.
    for line in lines[1:]:
        if not line:
            break
        if line.lower().startswith("transfer-encoding:") and "chunked" in line.lower():
            chunked = True

    path = url.path or "/"
    if url.query:
        path = f"{path}?{url.query}"
    out = [f"{method} {path} HTTP/1.1", f"Host: {rule.host}"]
    out.extend(f"{name}: {value}" for name, value in headers)
    if content_length:
        out.append(f"Content-Length: {content_length}")
    out.append(f"{rule.header}: {rule.secret}")
    out.append("Connection: close")
    head_bytes = ("\r\n".join(out) + "\r\n\r\n").encode("iso-8859-1")
    return ForwardedRequest(method, path, head_bytes, content_length, chunked)


async def forward_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    head: bytes,
    rule: InjectRule,
    connect_timeout_s: float,
) -> None:
    """Forward one plain-HTTP request to the rule's upstream over TLS.

    Streams the response back as it arrives (SSE needs incremental delivery).
    Returns a status string for logging; raises nothing on the happy path —
    connection errors are answered with a status line and swallowed like the
    CONNECT path's teardown.
    """
    parsed = parse_and_rewrite_request(head, rule)
    if isinstance(parsed, str):
        reasons = {
            "400": (400, "Bad Request"),
            "403": (403, "Forbidden"),
            "501": (501, "Not Implemented"),
        }
        code, reason = reasons[parsed]
        await _reply(writer, code, reason)
        return
    if parsed.chunked:
        await _reply(writer, 501, "Not Implemented")
        return

    ssl_ctx: ssl.SSLContext | None = None
    if rule.scheme == "https":
        ssl_ctx = ssl.create_default_context()
    try:
        upstream_r, upstream_w = await asyncio.wait_for(
            asyncio.open_connection(
                rule.host, rule.upstream_port, ssl=ssl_ctx, server_hostname=rule.host if ssl_ctx else None
            ),
            timeout=connect_timeout_s,
        )
    except (OSError, asyncio.TimeoutError):
        await _reply(writer, 502, "Bad Gateway")
        return

    try:
        upstream_w.write(parsed.head)
        await upstream_w.drain()
        # Request body: exactly Content-Length bytes (chunked rejected above).
        remaining = parsed.body_remaining
        while remaining > 0:
            chunk = await reader.read(min(64 * 1024, remaining))
            if not chunk:
                upstream_w.close()
                return  # client vanished mid-body; nothing useful to send
            upstream_w.write(chunk)
            remaining -= len(chunk)
        await upstream_w.drain()

        await _stream_response(upstream_r, writer)
    finally:
        upstream_w.close()


async def _stream_response(
    upstream_r: asyncio.StreamReader, client_w: asyncio.StreamWriter
) -> None:
    """Forward the upstream response head verbatim, then the body by framing.

    Framing order per RFC 9112: Transfer-Encoding: chunked wins over
    Content-Length; neither means body runs to EOF (we close, so that's
    well-defined for the client).
    """
    head = await _read_upstream_head(upstream_r)
    client_w.write(head)
    await client_w.drain()
    headers = _parse_response_headers(head)
    if headers.get("transfer-encoding", "").lower() == "chunked":
        await _pipe_chunked(upstream_r, client_w)
        return
    length_s = headers.get("content-length")
    if length_s is not None:
        try:
            remaining = int(length_s)
        except ValueError:
            remaining = 0
        while remaining > 0:
            chunk = await upstream_r.read(min(64 * 1024, remaining))
            if not chunk:
                break
            client_w.write(chunk)
            remaining -= len(chunk)
        await client_w.drain()
        return
    # No framing: body to EOF (SSE upstreams that close-delimit).
    while True:
        chunk = await upstream_r.read(64 * 1024)
        if not chunk:
            break
        client_w.write(chunk)
        await client_w.drain()


async def _read_upstream_head(reader: asyncio.StreamReader) -> bytes:
    """Read the response head. `readuntil` keeps any body bytes that arrived
    in the same segment buffered for the framing pipe — a plain `read` would
    over-consume them and corrupt the Content-Length/chunked accounting."""
    try:
        return await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError as exc:
        return exc.partial
    except asyncio.LimitOverrunError:
        # Pathological upstream head; drain what's buffered and forward it —
        # the framing pipe will treat the rest as body and likely fail, which
        # is honest for a broken upstream.
        return await reader.read(64 * 1024)


def _parse_response_headers(head: bytes) -> dict[str, str]:
    try:
        text = head.decode("iso-8859-1")
    except UnicodeDecodeError:
        return {}
    out: dict[str, str] = {}
    for line in text.split("\r\n")[1:]:
        if not line:
            break
        name, sep, value = line.partition(":")
        if sep:
            out[name.strip().lower()] = value.strip()
    return out


async def _pipe_chunked(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Forward chunked framing verbatim, flushing per chunk (SSE latency)."""
    while True:
        size_line = await reader.readline()
        if not size_line:
            return
        writer.write(size_line)
        try:
            size = int(size_line.strip().split(b";", 1)[0], 16)
        except ValueError:
            return  # malformed upstream framing; closing is the honest answer
        if size == 0:
            # Trailer section ends at the blank line.
            while True:
                line = await reader.readline()
                if not line:
                    return
                writer.write(line)
                if line == b"\r\n":
                    await writer.drain()
                    return
        remaining = size + 2  # chunk data + trailing CRLF
        while remaining > 0:
            chunk = await reader.read(min(64 * 1024, remaining))
            if not chunk:
                return
            writer.write(chunk)
            remaining -= len(chunk)
        await writer.drain()


async def _reply(writer: asyncio.StreamWriter, code: int, reason: str) -> None:
    writer.write(f"HTTP/1.1 {code} {reason}\r\ncontent-length: 0\r\n\r\n".encode())
    await writer.drain()
