"""Tests for the allow-listing CONNECT egress proxy.

Tunnel tests use a real loopback echo server as the CONNECT target — the
proxy's job is byte plumbing plus the allow-list gate, and both are only
honestly tested over real sockets.
"""

from __future__ import annotations

import asyncio

import pytest
from steerable_egress_proxy import (
    AllowList,
    EgressProxyServer,
    ProxyConfig,
    parse_allow_entry,
)
from steerable_egress_proxy.__main__ import main as cli_main


async def _start_echo() -> tuple[asyncio.AbstractServer, int]:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    return server, port


async def _start_proxy(allow: list[str]) -> tuple[EgressProxyServer, asyncio.Task, int]:
    server = EgressProxyServer(
        ProxyConfig(allow=AllowList(allow), bind_host="127.0.0.1", bind_port=0)
    )
    task = asyncio.create_task(server.serve())
    # Let serve() bind before we read the port.
    for _ in range(100):
        if server._server is not None:
            break
        await asyncio.sleep(0.01)
    return server, task, server.bound_port


async def _connect(
    proxy_port: int, authority: str
) -> tuple[bytes, asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(f"CONNECT {authority} HTTP/1.1\r\nhost: {authority}\r\n\r\n".encode())
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    return head, reader, writer


async def _raw_request(proxy_port: int, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(payload)
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    writer.close()
    return head


# ---- allow-list parsing ---------------------------------------------------


def test_bare_host_allows_443_and_80():
    entry = parse_allow_entry("api.deepseek.com")
    assert entry.allows("api.deepseek.com", 443)
    assert entry.allows("api.deepseek.com", 80)
    assert not entry.allows("api.deepseek.com", 22)


def test_host_port_entry_is_exact():
    entry = parse_allow_entry("localhost:11434")
    assert entry.allows("localhost", 11434)
    assert not entry.allows("localhost", 443)


def test_entry_matching_is_case_insensitive():
    entry = parse_allow_entry("API.Example.COM:8443")
    assert entry.allows("api.example.com", 8443)


@pytest.mark.parametrize(
    "raw",
    ["", ":", "host:", "host:0", "host:65536", "host:abc", "bad host:443", "h:o:s:t"],
)
def test_malformed_entries_raise(raw):
    with pytest.raises(ValueError):
        parse_allow_entry(raw)


def test_empty_allow_list_fails_loud():
    with pytest.raises(ValueError, match="empty"):
        AllowList([])


def test_proxy_config_requires_allow_list():
    # No default: constructing without `allow` is a type error; a None
    # smuggled past the type checker still fails loud.
    with pytest.raises((TypeError, ValueError)):
        ProxyConfig(allow=None)  # type: ignore[arg-type]


# ---- live tunneling -------------------------------------------------------


async def test_allowed_connect_tunnels_bytes_both_ways():
    echo, echo_port = await _start_echo()
    proxy, task, proxy_port = await _start_proxy([f"127.0.0.1:{echo_port}"])
    try:
        head, reader, writer = await _connect(proxy_port, f"127.0.0.1:{echo_port}")
        assert head.startswith(b"HTTP/1.1 200")
        writer.write(b"ping-through-proxy\n")
        await writer.drain()
        assert await reader.readline() == b"ping-through-proxy\n"
        writer.close()
    finally:
        await proxy.close()
        task.cancel()
        echo.close()


async def test_denied_host_gets_403_and_no_dial():
    proxy, task, proxy_port = await _start_proxy(["127.0.0.1:1"])
    try:
        head = await _raw_request(proxy_port, b"CONNECT 10.9.8.7:443 HTTP/1.1\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 403")
    finally:
        await proxy.close()
        task.cancel()


async def test_denied_port_gets_403():
    echo, echo_port = await _start_echo()
    proxy, task, proxy_port = await _start_proxy(["127.0.0.1:1"])
    try:
        head = await _raw_request(
            proxy_port, f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\n\r\n".encode()
        )
        assert head.startswith(b"HTTP/1.1 403")
    finally:
        await proxy.close()
        task.cancel()
        echo.close()


async def test_non_connect_method_gets_405():
    proxy, task, proxy_port = await _start_proxy(["127.0.0.1:80"])
    try:
        head = await _raw_request(
            proxy_port, b"GET http://example.com/ HTTP/1.1\r\n\r\n"
        )
        assert head.startswith(b"HTTP/1.1 405")
    finally:
        await proxy.close()
        task.cancel()


async def test_malformed_request_gets_400():
    proxy, task, proxy_port = await _start_proxy(["127.0.0.1:80"])
    try:
        head = await _raw_request(proxy_port, b"garbage\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 400")
    finally:
        await proxy.close()
        task.cancel()


async def test_oversized_head_gets_431():
    proxy, task, proxy_port = await _start_proxy(["127.0.0.1:80"])
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(
            b"CONNECT 127.0.0.1:80 HTTP/1.1\r\nx: " + b"a" * 32768 + b"\r\n\r\n"
        )
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 431")
        writer.close()
    finally:
        await proxy.close()
        task.cancel()


async def test_unreachable_allowed_target_gets_502():
    # Port 1 is allowed by the list but nothing listens there.
    proxy, task, proxy_port = await _start_proxy(["127.0.0.1:1"])
    try:
        head = await _raw_request(proxy_port, b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 502")
    finally:
        await proxy.close()
        task.cancel()


# ---- CLI ------------------------------------------------------------------


def test_cli_without_allow_exits_loud(capsys):
    assert cli_main(["--bind", "127.0.0.1:0"]) == 2
    assert "empty" in capsys.readouterr().err


def test_cli_with_bad_bind_exits_loud(capsys):
    assert cli_main(["--bind", "no-port", "--allow", "x:443"]) == 2
    assert "--bind" in capsys.readouterr().err


def test_cli_with_bad_allow_entry_exits_loud(capsys):
    assert cli_main(["--allow", "bad entry:443"]) == 2
    assert "invalid allow entry" in capsys.readouterr().err
