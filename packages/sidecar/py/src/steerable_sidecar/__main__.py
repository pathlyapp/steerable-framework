"""``python -m steerable_sidecar`` entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import logging

from .sidecar import Sidecar, SidecarConfig
from .web_tools import register_web_tools


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="steerable-sidecar")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Sidecar log level (logged on stderr).",
    )
    parser.add_argument(
        "--quiet-ready",
        action="store_true",
        help="Skip the __SIDECAR_READY__ stderr marker.",
    )
    parser.add_argument(
        "--storage-path",
        default=None,
        metavar="PATH",
        help="Persist sessions/traces/history to a sqlite database at PATH "
        "(W2.6.1). Default: in-memory, per-process only.",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    config = SidecarConfig(
        log_level=args.log_level,
        quiet_stderr=args.quiet_ready,
        storage_path=args.storage_path,
    )
    sidecar = Sidecar(config=config)
    # web_search / web_fetch on the RPC router: the desktop host delegates
    # these calls here over forward `tool.invoke` (single implementation —
    # the host carries schemas only). A malformed STEERABLE_WEB_* bound must
    # not brick chat for an optional feature, so the misconfiguration logs
    # loud and the sidecar serves without the web pair.
    try:
        register_web_tools(sidecar.tools)
    except ValueError as exc:
        logging.getLogger("steerable_sidecar").error(
            "web tools disabled: %s", exc
        )
    try:
        asyncio.run(sidecar.serve())
    except KeyboardInterrupt:
        logging.getLogger("steerable_sidecar").info("interrupted")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
