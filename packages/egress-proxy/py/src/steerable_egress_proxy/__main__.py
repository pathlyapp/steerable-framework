"""CLI: `steerable-egress-proxy --bind 127.0.0.1:8899 --allow api.deepseek.com ...`

Misconfiguration fails loud at startup: no `--allow` entries (or a
malformed one) exits non-zero before the socket opens — a proxy with an
unintended list is worse than no proxy.

Credential broker (W2.2.2): `--inject-host HOST --inject-secret-env VAR`
turns on plain-HTTP forwarding to HOST with the credential header injected
from the env var's value. The secret never appears in argv (visible in
`ps`) — only the env var *name* does.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .forward import InjectRule
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
    parser.add_argument(
        "--inject-host",
        metavar="HOST",
        help=(
            "credential broker: forward plain-HTTP requests naming HOST to "
            "HOST over TLS with the credential header injected. Requires "
            "--inject-secret-env."
        ),
    )
    parser.add_argument(
        "--inject-secret-env",
        metavar="VAR",
        help=(
            "env var holding the full credential header value (e.g. "
            "'Bearer sk-...'). The value never appears in argv."
        ),
    )
    parser.add_argument(
        "--inject-header",
        default="Authorization",
        help="header to inject (default Authorization; e.g. x-api-key)",
    )
    parser.add_argument(
        "--inject-scheme",
        choices=("https", "http"),
        default="https",
        help="upstream scheme (default https; http is for tests/loopback)",
    )
    parser.add_argument(
        "--inject-port",
        type=int,
        default=None,
        help="upstream port (default 443 for https, 80 for http)",
    )
    args = parser.parse_args(argv)

    bind_host, sep, bind_port_s = args.bind.rpartition(":")
    if not sep or not bind_host:
        print(f"error: --bind must be host:port, got {args.bind!r}", file=sys.stderr)
        return 2
    inject: InjectRule | None = None
    if args.inject_host or args.inject_secret_env:
        if not args.inject_host or not args.inject_secret_env:
            print(
                "error: --inject-host and --inject-secret-env must come together",
                file=sys.stderr,
            )
            return 2
        secret = os.environ.get(args.inject_secret_env, "")
        if not secret:
            print(
                f"error: inject secret env var {args.inject_secret_env!r} is empty or unset",
                file=sys.stderr,
            )
            return 2
        try:
            inject = InjectRule(
                host=args.inject_host,
                secret=secret,
                header=args.inject_header,
                scheme=args.inject_scheme,
                port=args.inject_port,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    try:
        bind_port = int(bind_port_s)
        allow = AllowList(args.allow)
        config = ProxyConfig(
            allow=allow,
            bind_host=bind_host,
            bind_port=bind_port,
            connect_timeout_s=args.connect_timeout,
            inject=inject,
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
