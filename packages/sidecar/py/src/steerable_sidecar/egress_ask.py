"""Session-scoped egress widening via the host approval channel.

When the egress proxy refuses a CONNECT (403), the web tools can ask the
host UI whether to allow the target instead of returning a dead-end error.
An allow decision is relayed to the proxy's loopback control endpoint as a
session-scoped allow-list addition (it dies with the proxy process), and
the original request is retried once.

The security boundary is the control token: it reaches the sidecar's
environment but never the sandboxed children's (their environment is
scrubbed to a small allowlist), so a confined process cannot widen its own
egress. Without an approval channel (headless / auto mode) every denial
stays a denial — the asker fails closed.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from steerable_agent_runtime import ApprovalRequest
from steerable_agent_runtime.transport.stdio_jsonrpc import JsonRpcServer

from .host_tools import HostApprover

logger = logging.getLogger(__name__)

#: Matches the proxy's 403 reason phrase (``proxy.py``): the only metadata
#: channel a CONNECT client can see.
_EGRESS_DENIED_RE = re.compile(r"egress denied for ([A-Za-z0-9._-]+):(\d{1,5})")

#: Users read the prompt at human speed; a short timeout would turn a slow
#: read into a spurious denial.
_DEFAULT_ASK_TIMEOUT_S = 180.0

_ALLOW_KINDS = frozenset({"allow_once", "allow_for_session", "allow_always"})


def parse_egress_denied(error: BaseException) -> tuple[str, int] | None:
    """Extract ``(host, port)`` from a proxy 403, or None for other errors."""
    match = _EGRESS_DENIED_RE.search(str(error))
    if not match:
        return None
    return match.group(1), int(match.group(2))


class EgressApprovalAsker:
    """Ask the host to widen egress for one denied target, then relay the
    grant to the proxy's control endpoint.

    ``session_denied`` remembers refusals so a repeated fetch of a denied
    host fails fast instead of re-prompting every round. Allow decisions
    need no cache: the proxy's session set already remembers them.
    """

    def __init__(
        self,
        server: JsonRpcServer,
        *,
        control_port: int,
        control_token: str,
        timeout_s: float = _DEFAULT_ASK_TIMEOUT_S,
    ) -> None:
        self._server = server
        self._control_port = control_port
        self._control_token = control_token
        self._timeout_s = timeout_s
        self._session_denied: set[str] = set()

    async def ask_and_allow(self, host: str, port: int, url: str) -> bool:
        """Prompt the host; on allow, widen the proxy and return True."""
        key = f"{host}:{port}"
        if key in self._session_denied:
            return False
        approver = HostApprover(self._server, timeout=self._timeout_s)
        try:
            decision = await approver.approve(
                ApprovalRequest(
                    tool_name="network_egress",
                    arguments={"host": host, "port": port, "url": url},
                    mode="other",
                    category="network_egress",
                )
            )
        except Exception as exc:  # noqa: BLE001 — fail closed
            logger.warning("egress approval request failed: %s", exc)
            return False
        if decision.kind not in _ALLOW_KINDS:
            self._session_denied.add(key)
            return False
        return await self._relay_allow(host, port)

    async def _relay_allow(self, host: str, port: int) -> bool:
        import httpx  # local import — sidecar stays importable without it

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"http://127.0.0.1:{self._control_port}/allow",
                    headers={"Authorization": f"Bearer {self._control_token}"},
                    json={"host": f"{host}:{port}"},
                )
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("egress control endpoint unreachable: %s", exc)
            return False
        if response.status_code != 200:
            logger.warning("egress control endpoint refused: %s", response.status_code)
            return False
        return True


def asker_from_environ(
    server: JsonRpcServer, environ: dict[str, str] | Any
) -> EgressApprovalAsker | None:
    """Build an asker when the proxy's control endpoint is configured.

    The launcher (desktop boot, headless supervisor) injects both values
    after binding the proxy's ephemeral control port; their absence means
    no control plane exists and denials stay denials.
    """
    import os

    env = os.environ if environ is None else environ
    port_raw = (env.get("STEERABLE_EGRESS_CONTROL_PORT") or "").strip()
    token = (env.get("STEERABLE_EGRESS_CONTROL_TOKEN") or "").strip()
    if not port_raw or not token:
        return None
    try:
        port = int(port_raw)
    except ValueError:
        logger.warning("invalid STEERABLE_EGRESS_CONTROL_PORT: %r", port_raw)
        return None
    return EgressApprovalAsker(server, control_port=port, control_token=token)
