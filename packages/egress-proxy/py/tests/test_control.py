"""Tests for the proxy's session-scoped widening: the loopback control
endpoint, the 403 reason phrase naming the denied target, and AllowList.add.

The control endpoint is the only way a host-approved grant reaches the
proxy; its bearer token is the whole authorization story, so the negative
paths (no token, wrong token, wrong path, bad body) matter as much as the
happy path.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from steerable_egress_proxy import (
    AllowList,
    EgressProxyServer,
    ProxyConfig,
)


async def _start_proxy(
    allow: list[str], *, control_token: str | None = None
) -> tuple[EgressProxyServer, asyncio.Task, int]:
    server = EgressProxyServer(
        ProxyConfig(
            allow=AllowList(allow),
            bind_host="127.0.0.1",
            bind_port=0,
            control_token=control_token,
            control_port=0,
        )
    )
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server._server is not None:
            break
        await asyncio.sleep(0.01)
    return server, task, server.bound_port


async def _connect_request(port: int, target: str) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    writer.close()
    return head


async def _control_request(
    port: int, raw: bytes
) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    try:
        length = int(
            next(
                (
                    line.partition(b":")[2].strip()
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ),
                b"0",
            )
        )
        body = await reader.readexactly(length) if length else b""
    finally:
        writer.close()
    return head + body


def _allow_post(token: str, host: str) -> bytes:
    body = json.dumps({"host": host}).encode()
    return (
        b"POST /allow HTTP/1.1\r\n"
        + f"authorization: Bearer {token}\r\n".encode()
        + b"content-type: application/json\r\n"
        + f"content-length: {len(body)}\r\n".encode()
        + b"\r\n"
        + body
    )


class TestAllowListAdd:
    def test_runtime_addition_widens_allows(self) -> None:
        allow = AllowList(["api.github.com"])
        assert not allow.allows("example.com", 443)
        allow.add("example.com")
        assert allow.allows("example.com", 443)
        assert allow.allows("example.com", 80)
        assert not allow.allows("example.com", 8080)

    def test_baseline_entries_stay_immutable(self) -> None:
        allow = AllowList(["api.github.com"])
        allow.add("example.com:8443")
        assert [e.host for e in allow.entries] == ["api.github.com"]

    def test_add_validates_like_baseline(self) -> None:
        allow = AllowList(["api.github.com"])
        with pytest.raises(ValueError, match="invalid allow entry"):
            allow.add("bad host!")


class TestDeniedReasonNamesTarget:
    @pytest.mark.asyncio
    async def test_403_reason_carries_host_and_port(self) -> None:
        server, task, port = await _start_proxy(["api.github.com"])
        try:
            head = await _connect_request(port, "evil.example.com:443")
            assert head.startswith(
                b"HTTP/1.1 403 Forbidden; egress denied for evil.example.com:443"
            )
        finally:
            await server.close()
            task.cancel()


class TestControlEndpoint:
    @pytest.mark.asyncio
    async def test_allow_then_connect_succeeds(self) -> None:
        server, task, port = await _start_proxy(
            ["api.github.com"], control_token="tok-1"
        )
        try:
            for _ in range(100):
                if server.bound_control_port is not None:
                    break
                await asyncio.sleep(0.01)
            control = server.bound_control_port
            assert control is not None

            # Denied before the grant. 192.0.2.1 is TEST-NET-1 (RFC 5737):
            # unroutable, so the post-grant dial fails fast with 502 instead
            # of depending on real external reachability.
            head = await _connect_request(port, "192.0.2.1:443")
            assert head.startswith(b"HTTP/1.1 403")

            response = await _control_request(control, _allow_post("tok-1", "192.0.2.1"))
            assert response.startswith(b"HTTP/1.1 200")
            assert b'"allowed": "192.0.2.1"' in response

            # The CONNECT now passes the gate (the dial fails — TEST-NET-1
            # is unreachable — but 502 proves the allow-list let it through).
            head = await _connect_request(port, "192.0.2.1:443")
            assert head.startswith(b"HTTP/1.1 502")
        finally:
            await server.close()
            task.cancel()

    @pytest.mark.asyncio
    async def test_missing_token_gets_401(self) -> None:
        server, task, _ = await _start_proxy(["api.github.com"], control_token="tok-1")
        try:
            for _ in range(100):
                if server.bound_control_port is not None:
                    break
                await asyncio.sleep(0.01)
            body = b'{"host": "example.com"}'
            response = await _control_request(
                server.bound_control_port or 0,
                b"POST /allow HTTP/1.1\r\n"
                + f"content-length: {len(body)}\r\n".encode()
                + b"\r\n"
                + body,
            )
            assert response.startswith(b"HTTP/1.1 401")
        finally:
            await server.close()
            task.cancel()

    @pytest.mark.asyncio
    async def test_wrong_token_gets_401(self) -> None:
        server, task, _ = await _start_proxy(["api.github.com"], control_token="tok-1")
        try:
            for _ in range(100):
                if server.bound_control_port is not None:
                    break
                await asyncio.sleep(0.01)
            response = await _control_request(
                server.bound_control_port or 0, _allow_post("wrong", "example.com")
            )
            assert response.startswith(b"HTTP/1.1 401")
        finally:
            await server.close()
            task.cancel()

    @pytest.mark.asyncio
    async def test_unknown_path_gets_404(self) -> None:
        server, task, _ = await _start_proxy(["api.github.com"], control_token="tok-1")
        try:
            for _ in range(100):
                if server.bound_control_port is not None:
                    break
                await asyncio.sleep(0.01)
            body = b'{"host": "example.com"}'
            response = await _control_request(
                server.bound_control_port or 0,
                b"POST /admin HTTP/1.1\r\n"
                + b"authorization: Bearer tok-1\r\n"
                + f"content-length: {len(body)}\r\n".encode()
                + b"\r\n"
                + body,
            )
            assert response.startswith(b"HTTP/1.1 404")
        finally:
            await server.close()
            task.cancel()

    @pytest.mark.asyncio
    async def test_malformed_host_gets_400(self) -> None:
        server, task, _ = await _start_proxy(["api.github.com"], control_token="tok-1")
        try:
            for _ in range(100):
                if server.bound_control_port is not None:
                    break
                await asyncio.sleep(0.01)
            response = await _control_request(
                server.bound_control_port or 0, _allow_post("tok-1", "bad host!")
            )
            assert response.startswith(b"HTTP/1.1 400")
        finally:
            await server.close()
            task.cancel()

    @pytest.mark.asyncio
    async def test_no_token_configured_means_no_control_plane(self) -> None:
        server, task, _ = await _start_proxy(["api.github.com"])
        try:
            await asyncio.sleep(0.05)
            assert server.bound_control_port is None
        finally:
            await server.close()
            task.cancel()
