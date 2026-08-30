"""Tests for the credential-broker forward path (W2.2.2).

The upstream is a real loopback HTTP server that records exactly what it
received — the broker's contract is byte-level (header strip + inject +
origin-form rewrite), so the tests assert on captured wire bytes, not mocks.
"""

from __future__ import annotations

import asyncio

import pytest
from steerable_egress_proxy import (
    AllowList,
    EgressProxyServer,
    ProxyConfig,
)
from steerable_egress_proxy.__main__ import main as cli_main
from steerable_egress_proxy.forward import InjectRule, parse_and_rewrite_request

RULE = InjectRule(host="127.0.0.1", secret="Bearer test-key", scheme="http")


class RecordingUpstream:
    """Minimal HTTP/1.1 server capturing raw requests and serving a canned
    response (content-length or scripted chunked)."""

    def __init__(self) -> None:
        self.requests: list[bytes] = []
        self.response_head = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n"
        self.response_body = b"ok"
        self.chunked_script: list[bytes] | None = None
        self.chunk_written = asyncio.Event()

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return int(self._server.sockets[0].getsockname()[1])

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        body = b""
        for line in head.decode("iso-8859-1").split("\r\n"):
            if line.lower().startswith("content-length:"):
                n = int(line.partition(":")[2].strip())
                body = await reader.readexactly(n)
        self.requests.append(head + body)
        if self.chunked_script is not None:
            writer.write(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                b"Content-Type: text/event-stream\r\n\r\n"
            )
            await writer.drain()
            for chunk in self.chunked_script:
                payload = f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n"
                writer.write(payload)
                await writer.drain()
                self.chunk_written.set()
                await asyncio.sleep(0.05)
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        else:
            writer.write(self.response_head + self.response_body)
            await writer.drain()
        writer.close()


async def _start_proxy(
    inject: InjectRule | None = None,
    allow: list[str] | None = None,
) -> tuple[EgressProxyServer, asyncio.Task, int]:
    server = EgressProxyServer(
        ProxyConfig(
            allow=AllowList(allow or ["127.0.0.1"]),
            bind_host="127.0.0.1",
            bind_port=0,
            inject=inject,
        )
    )
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server._server is not None:
            break
        await asyncio.sleep(0.01)
    return server, task, server.bound_port


async def _request(proxy_port: int, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(payload)
    await writer.drain()
    out = b""
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            break
        out += chunk
    return out


# ---- pure rewrite rules ---------------------------------------------------


def test_rewrite_strips_client_credential_and_injects_rule_secret():
    head = (
        b"POST http://127.0.0.1/v1/chat/completions?stream=true HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Authorization: Bearer client-side-fake\r\n"
        b"Proxy-Authorization: Basic abc\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 11\r\n"
        b"Connection: keep-alive\r\n\r\n"
    )
    parsed = parse_and_rewrite_request(head, RULE)
    assert not isinstance(parsed, str)
    text = parsed.head.decode()
    assert text.startswith("POST /v1/chat/completions?stream=true HTTP/1.1\r\n")
    assert "Authorization: Bearer test-key\r\n" in text
    assert "client-side-fake" not in text
    assert "Proxy-Authorization" not in text
    assert "keep-alive" not in text
    assert "Content-Length: 11\r\n" in text
    assert parsed.body_remaining == 11


def test_rewrite_rejects_off_host_and_non_http_targets():
    assert parse_and_rewrite_request(
        b"POST http://evil.example.com/v1 HTTP/1.1\r\n\r\n", RULE
    ) == "403"
    assert parse_and_rewrite_request(
        b"POST /v1/origin-form HTTP/1.1\r\n\r\n", RULE
    ) == "403"


def test_rewrite_flags_chunked_request_for_501():
    head = (
        b"POST http://127.0.0.1/v1 HTTP/1.1\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
    )
    parsed = parse_and_rewrite_request(head, RULE)
    assert not isinstance(parsed, str)
    assert parsed.chunked is True


def test_inject_rule_requires_host_and_secret():
    with pytest.raises(ValueError):
        InjectRule(host="", secret="x")
    with pytest.raises(ValueError):
        InjectRule(host="h", secret="")
    with pytest.raises(ValueError):
        InjectRule(host="h", secret="x", scheme="gopher")


# ---- end-to-end over real sockets -----------------------------------------


async def test_forward_injects_and_streams_response():
    upstream = RecordingUpstream()
    upstream_port = await upstream.start()
    rule = InjectRule(
        host="127.0.0.1", secret="Bearer test-key", scheme="http", port=upstream_port
    )
    server, task, proxy_port = await _start_proxy(inject=rule)
    try:
        body = b'{"msg":"hi"}'
        response = await _request(
            proxy_port,
            b"POST http://127.0.0.1/v1/chat/completions HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer should-never-arrive\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body,
        )
        assert response.startswith(b"HTTP/1.1 200 OK")
        assert response.endswith(b"ok")
        received = upstream.requests[0]
        assert b"Authorization: Bearer test-key" in received
        assert b"should-never-arrive" not in received
        assert received.endswith(body)
    finally:
        task.cancel()
        await server.close()


async def test_forward_denies_off_host_with_403():
    server, task, proxy_port = await _start_proxy(inject=RULE)
    try:
        response = await _request(
            proxy_port,
            b"GET http://not-the-rule-host.example/ HTTP/1.1\r\n\r\n",
        )
        assert response.startswith(b"HTTP/1.1 403")
    finally:
        task.cancel()
        await server.close()


async def test_plain_http_stays_405_without_inject_rule():
    server, task, proxy_port = await _start_proxy(inject=None)
    try:
        response = await _request(
            proxy_port, b"GET http://127.0.0.1/ HTTP/1.1\r\n\r\n"
        )
        assert response.startswith(b"HTTP/1.1 405")
    finally:
        task.cancel()
        await server.close()


async def test_chunked_request_body_fails_loud_501():
    server, task, proxy_port = await _start_proxy(inject=RULE)
    try:
        response = await _request(
            proxy_port,
            b"POST http://127.0.0.1/v1 HTTP/1.1\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"4\r\nWiki\r\n0\r\n\r\n",
        )
        assert response.startswith(b"HTTP/1.1 501")
    finally:
        task.cancel()
        await server.close()


async def test_chunked_sse_response_streams_incrementally():
    """The first SSE chunk must arrive before the upstream finishes — proves
    the pipe is incremental, not buffered (streaming latency is the product
    requirement for LLM responses)."""
    upstream = RecordingUpstream()
    upstream.chunked_script = [b"data: one\n\n", b"data: two\n\n"]
    upstream_port = await upstream.start()
    rule = InjectRule(
        host="127.0.0.1", secret="Bearer k", scheme="http", port=upstream_port
    )
    server, task, proxy_port = await _start_proxy(inject=rule)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(b"POST http://127.0.0.1/v1/chat HTTP/1.1\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        first = await reader.readuntil(b"data: one\n\n")
        assert b"data: one" in first
        # The upstream is still mid-script (it sleeps between chunks) — the
        # first chunk reached us before the response completed.
        rest = b""
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            rest += chunk
        assert b"data: two" in rest
        assert rest.endswith(b"0\r\n\r\n")
        writer.close()
    finally:
        task.cancel()
        await server.close()


async def test_connect_path_unaffected_by_inject_rule():
    """CONNECT tunneling keeps working when the broker is configured."""
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            data = await reader.read(100)
            if not data:
                break
            writer.write(data)
            await writer.drain()
        writer.close()

    echo_server = await asyncio.start_server(echo, "127.0.0.1", 0)
    echo_port = int(echo_server.sockets[0].getsockname()[1])
    # Bare-host allow entries cover 443/80 only — pin the echo port.
    server, task, proxy_port = await _start_proxy(
        inject=RULE, allow=[f"127.0.0.1:{echo_port}"]
    )
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(
            f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\nhost: x\r\n\r\n".encode()
        )
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        assert b"200" in head
        writer.write(b"ping")
        await writer.drain()
        assert await reader.readexactly(4) == b"ping"
        writer.close()
    finally:
        task.cancel()
        await server.close()
        echo_server.close()


# ---- CLI fail-loud ----------------------------------------------------------


def test_cli_inject_flags_must_come_together(capsys):
    code = cli_main(["--allow", "x", "--inject-host", "api.example.com"])
    assert code == 2
    assert "must come together" in capsys.readouterr().err


def test_cli_inject_secret_env_must_be_set(capsys, monkeypatch):
    monkeypatch.delenv("STEERABLE_TEST_MISSING_SECRET", raising=False)
    code = cli_main(
        [
            "--allow",
            "x",
            "--inject-host",
            "api.example.com",
            "--inject-secret-env",
            "STEERABLE_TEST_MISSING_SECRET",
        ]
    )
    assert code == 2
    assert "empty or unset" in capsys.readouterr().err


def test_cli_inject_happy_path_binds_and_serves(monkeypatch):
    """Secret resolves from env; the rule lands on the config."""
    monkeypatch.setenv("STEERABLE_TEST_SECRET", "Bearer env-key")
    captured = {}

    class FakeServer:
        def __init__(self, config):
            captured["config"] = config

        async def serve(self):
            raise asyncio.CancelledError()

        async def close(self):
            pass

    monkeypatch.setattr(
        "steerable_egress_proxy.__main__.EgressProxyServer", FakeServer
    )
    code = cli_main(
        [
            "--allow",
            "api.example.com",
            "--inject-host",
            "api.example.com",
            "--inject-secret-env",
            "STEERABLE_TEST_SECRET",
        ]
    )
    assert code == 0
    rule = captured["config"].inject
    assert rule.host == "api.example.com"
    assert rule.secret == "Bearer env-key"
    assert rule.scheme == "https"
    assert rule.upstream_port == 443
