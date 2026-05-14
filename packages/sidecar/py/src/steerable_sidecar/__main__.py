"""``python -m steerable_sidecar`` entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import logging

from .sidecar import Sidecar, SidecarConfig


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
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    config = SidecarConfig(log_level=args.log_level, quiet_stderr=args.quiet_ready)
    sidecar = Sidecar(config=config)
    try:
        asyncio.run(sidecar.serve())
    except KeyboardInterrupt:
        logging.getLogger("steerable_sidecar").info("interrupted")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
