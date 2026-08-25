"""Test-only sidecar that exercises the reverse (sidecar -> host) channel.

Run as a subprocess: ``python -m tests.reverse_echo_sidecar``. It registers one
extra method, ``test.run_host_tool``, which — when the host calls it — makes the
sidecar issue a *reverse* ``tool.invoke`` request back to the host and returns
whatever the host responded. This proves the bidirectional request path over
real stdio.
"""

from __future__ import annotations

import asyncio
from typing import Any

from steerable_sidecar import Sidecar, SidecarConfig


def build_sidecar() -> Sidecar:
    sidecar = Sidecar(config=SidecarConfig(log_level="ERROR"))

    async def run_host_tool(params: dict[str, Any] | None) -> Any:
        params = params or {}
        # Reverse call: ask the host to execute a tool and await its result.
        return await sidecar.server.call(
            "tool.invoke",
            {
                "name": params.get("name", "local_exec_shell"),
                "arguments": params.get("arguments") or {},
            },
            timeout=10.0,
        )

    sidecar.server.register("test.run_host_tool", run_host_tool)
    return sidecar


def main() -> int:
    asyncio.run(build_sidecar().serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
