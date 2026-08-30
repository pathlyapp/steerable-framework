"""Allow-listing CONNECT forward proxy + optional credential broker.

The proxy accepts ``CONNECT host:port`` requests, checks the target against
an explicit allow-list, dials, and pipes bytes bidirectionally. Everything
else fails closed:

- target not on the list            → 403
- non-CONNECT method w/o inject rule → 405
- non-CONNECT to a non-inject host  → 403
- malformed request line / headers  → 400
- unreachable target                → 502

TLS is NOT intercepted on the CONNECT path: the proxy sees the CONNECT
target only, which is exactly the metadata the host allow-list needs.

When an ``InjectRule`` is configured (W2.2.2 credential broker), plain-HTTP
requests naming that host in the absolute URI are forwarded over TLS with
the credential header injected — see `forward.py`. The secret lives only in
this process; the sandboxed peer never holds it.

Bounds that keep the security boundary tight:

- request head capped at ``MAX_HEAD_BYTES`` (16 KiB) → 431 over cap
- CONNECT dial bounded by ``connect_timeout_s``
- an empty allow-list is a configuration error, not "open" — constructing
  ``AllowList([])`` raises ``ValueError`` (fail loud, never silently open)
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from .forward import InjectRule, forward_request

logger = logging.getLogger(__name__)

MAX_HEAD_BYTES = 16 * 1024
_DEFAULT_CONNECT_TIMEOUT_S = 10.0
#: Ports implied by a bare `host` allow entry — mirrors the Seatbelt
#: profile semantics in docs/spec/safety.md so the two layers agree.
_BARE_HOST_PORTS = frozenset({443, 80})

_ENTRY_RE = re.compile(
    r"^(?P<host>[A-Za-z0-9._-]+|\[[0-9a-fA-F:]+\])(?::(?P<port>\d{1,5}))?$"
)


@dataclass(frozen=True, slots=True)
class AllowEntry:
    host: str
    ports: frozenset[int]  # empty frozenset is never stored; see parse

    def allows(self, host: str, port: int) -> bool:
        return self.host == host.lower() and port in self.ports


def parse_allow_entry(raw: str) -> AllowEntry:
    """Parse one `host` or `host:port` entry. Bare hosts allow 443 and 80.

    Raises ValueError on anything malformed — a bad entry must never
    silently widen or narrow the list.
    """
    text = raw.strip()
    m = _ENTRY_RE.match(text)
    if not m:
        raise ValueError(f"invalid allow entry {raw!r}: expected host or host:port")
    host = m.group("host").lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    port_s = m.group("port")
    if port_s is None:
        return AllowEntry(host, frozenset(_BARE_HOST_PORTS))
    port = int(port_s)
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid allow entry {raw!r}: port out of range")
    return AllowEntry(host, frozenset({port}))


class AllowList:
    """Closed set of allowed CONNECT targets. Empty input is an error."""

    def __init__(self, entries: list[str]):
        if not entries:
            raise ValueError(
                "allow-list is empty: an egress proxy with no entries is "
                "either a mistake or should not be run at all"
            )
        self._entries = tuple(parse_allow_entry(e) for e in entries)

    @property
    def entries(self) -> tuple[AllowEntry, ...]:
        return self._entries

    def allows(self, host: str, port: int) -> bool:
        host = host.lower()
        return any(e.allows(host, port) for e in self._entries)


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    allow: AllowList  # required — fail-closed by construction
    bind_host: str = "127.0.0.1"
    bind_port: int = 8899
    connect_timeout_s: float = _DEFAULT_CONNECT_TIMEOUT_S
    #: W2.2.2 credential broker. None → non-CONNECT methods stay 405.
    inject: InjectRule | None = None

    def __post_init__(self) -> None:
        if self.allow is None:
            raise ValueError("ProxyConfig.allow is required (fail-closed)")
        # 0 is valid: ephemeral bind (tests, supervised spawns that read the
        # port back from `bound_port`).
        if not 0 <= self.bind_port <= 65535:
            raise ValueError(f"bind_port out of range: {self.bind_port}")


class EgressProxyServer:
    """Asyncio CONNECT proxy. `await serve()` runs until cancelled."""

    def __init__(self, config: ProxyConfig):
        self.config = config
        self._server: asyncio.AbstractServer | None = None

    @property
    def bound_port(self) -> int:
        if self._server and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self.config.bind_port

    async def serve(self) -> None:
        self._server = await asyncio.start_server(
            self._handle,
            self.config.bind_host,
            self.config.bind_port,
        )
        logger.info(
            "egress proxy on %s:%s (%d allow entries)",
            self.config.bind_host,
            self.bound_port,
            len(self.config.allow.entries),
        )
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # ---- connection handling -------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            head = await self._read_head(reader)
            if head is None:
                await self._reply(writer, 431, "Request Header Fields Too Large")
                return
            parsed = self._parse_request_line(head)
            if parsed is None:
                await self._reply(writer, 400, "Bad Request")
                return
            method, host, port = parsed
            if method != "CONNECT":
                if self.config.inject is not None:
                    # Credential-broker path: forward plain HTTP to the rule's
                    # upstream with the secret injected. Off-host requests and
                    # malformed heads are answered inside (403/400/501).
                    await forward_request(
                        reader,
                        writer,
                        head,
                        self.config.inject,
                        self.config.connect_timeout_s,
                    )
                    return
                # Checked before authority validity: a GET with a weird
                # target is still a 405, not a 400.
                await self._reply(writer, 405, "Method Not Allowed")
                return
            if host is None or port is None:
                await self._reply(writer, 400, "Bad Request")
                return
            if not self.config.allow.allows(host, port):
                logger.info("deny %s:%d (not on allow-list)", host, port)
                await self._reply(writer, 403, "Forbidden")
                return
            try:
                upstream_r, upstream_w = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=self.config.connect_timeout_s,
                )
            except (OSError, asyncio.TimeoutError):
                await self._reply(writer, 502, "Bad Gateway")
                return
            await self._reply(writer, 200, "Connection Established")
            await self._tunnel(reader, writer, upstream_r, upstream_w)
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass  # client or upstream hung up mid-stream — normal teardown
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass  # peer already gone

    async def _read_head(self, reader: asyncio.StreamReader) -> bytes | None:
        """Read up to the blank line ending the request head; None = over cap.

        Uses `readuntil`, not `read`: anything past the head delimiter (e.g.
        the request body, which the credential-broker path forwards by exact
        Content-Length) must stay in the StreamReader buffer for the next
        reader. A plain `read` would over-consume the body and deadlock the
        forward path.
        """
        try:
            data = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError as exc:
            return exc.partial if exc.partial else b""
        except asyncio.LimitOverrunError:
            return None
        if len(data) > MAX_HEAD_BYTES:
            return None
        return data

    @staticmethod
    def _parse_request_line(head: bytes) -> tuple[str, str | None, int | None] | None:
        """Split the request line. `(method, None, None)` means the method is
        fine but the authority is not a CONNECT target — caller maps that to
        405-before-400 ordering."""
        try:
            first = head.split(b"\r\n", 1)[0].decode("ascii")
        except UnicodeDecodeError:
            return None
        parts = first.split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/"):
            return None
        method, authority = parts[0].upper(), parts[1]
        if ":" not in authority:
            return (method, None, None)
        host, _, port_s = authority.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            return (method, None, None)
        if not host or not 1 <= port <= 65535:
            return (method, None, None)
        return method, host.lower(), port

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, code: int, reason: str) -> None:
        writer.write(f"HTTP/1.1 {code} {reason}\r\ncontent-length: 0\r\n\r\n".encode())
        await writer.drain()

    async def _tunnel(
        self,
        client_r: asyncio.StreamReader,
        client_w: asyncio.StreamWriter,
        upstream_r: asyncio.StreamReader,
        upstream_w: asyncio.StreamWriter,
    ) -> None:
        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            while True:
                chunk = await src.read(64 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
                await dst.drain()
            if dst.can_write_eof():
                dst.write_eof()

        up = asyncio.create_task(pipe(client_r, upstream_w))
        down = asyncio.create_task(pipe(upstream_r, client_w))
        try:
            await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (up, down):
                t.cancel()
            upstream_w.close()
