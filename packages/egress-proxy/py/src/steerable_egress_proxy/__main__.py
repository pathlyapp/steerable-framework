"""CLI: `steerable-egress-proxy --bind 127.0.0.1:8899 --allow api.deepseek.com ...`

Misconfiguration fails loud at startup: no `--allow` entries (or a
malformed one) exits non-zero before the socket opens — a proxy with an
unintended list is worse than no proxy.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .proxy import AllowList, EgressProxyServer, ProxyConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="steerable-egress-proxy",
        description="Local allow-listing CONNECT egress proxy (v1: no TLS interception).",
    )
    parser.add_argument(
        "--bind",
        default="127.0.0.1:8899",
        help="listen address (default 127.0.0.1:8899)",
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="HOST[:PORT]",
        help="allowed CONNECT target; repeatable. Bare host allows 443 and 80.",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=10.0,
        help="upstream dial timeout in seconds (default 10)",
    )
    args = parser.parse_args(argv)

    bind_host, sep, bind_port_s = args.bind.rpartition(":")
    if not sep or not bind_host:
        print(f"error: --bind must be host:port, got {args.bind!r}", file=sys.stderr)
        return 2
    try:
        bind_port = int(bind_port_s)
        allow = AllowList(args.allow)
        config = ProxyConfig(
            allow=allow,
            bind_host=bind_host,
            bind_port=bind_port,
            connect_timeout_s=args.connect_timeout,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    server = EgressProxyServer(config)

    async def run() -> None:
        try:
            await server.serve()
        except asyncio.CancelledError:
            await server.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
