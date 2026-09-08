"""``python -m steerable_sidecar`` entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from steerable_agent_runtime.errors import StoreAlreadyOwnedError

from .sidecar import Sidecar, SidecarConfig
from .web_tools import register_web_tools
from .run_code import register_run_code, run_code_enabled
from .ptc_js import ptc_js_enabled, register_ptc_js
from .todo_tools import register_todo_write


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
    try:
        sidecar = Sidecar(config=config)
    except StoreAlreadyOwnedError as exc:
        logging.getLogger("steerable_sidecar").error("%s", exc)
        return 1
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
    if run_code_enabled():
        register_run_code(sidecar.tools)
    if ptc_js_enabled():
        register_ptc_js(sidecar.tools)
    # todo_write: session task list (CC TodoWrite parity). Unconditional —
    # no workspace side effects, no host wiring, no env gate.
    register_todo_write(sidecar.tools)
    # Third-party tools: load plugins from every configured source —
    # installed packages declaring the ``steerable.tools`` entry-point
    # group, plus the local development directory named by
    # STEERABLE_PLUGIN_DIR. A broken plugin fails the boot loud
    # (PluginLoadError names the offender) rather than silently dropping an
    # installed tool. The registry stays alive for the process lifetime and
    # backs the plugin.* management RPCs.
    from steerable_agent_runtime import (
        DirectorySource,
        EntryPointSource,
        PluginLoadError,
        PluginRegistry,
        PluginSource,
    )

    plugins = PluginRegistry(sidecar.tools)
    sources: list[PluginSource] = [EntryPointSource()]
    plugin_dir = os.environ.get("STEERABLE_PLUGIN_DIR")
    if plugin_dir:
        sources.append(DirectorySource(plugin_dir))
    try:
        for source in sources:
            plugins.load_source(source)
    except PluginLoadError as exc:
        logging.getLogger("steerable_sidecar").error("tool plugin: %s", exc)
        return 1
    try:
        asyncio.run(sidecar.serve())
    except KeyboardInterrupt:
        logging.getLogger("steerable_sidecar").info("interrupted")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
