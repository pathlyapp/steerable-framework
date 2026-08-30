"""steerable-egress-proxy — local allow-listing CONNECT egress proxy.

Optional component. See docs/spec/safety.md "Egress allow-list": OS
sandboxes degrade to port-level (Seatbelt) or namespace-level (bwrap)
egress; per-host enforcement needs a local proxy that owns the host list.
"""

from .proxy import (
    AllowList,
    EgressProxyServer,
    ProxyConfig,
    parse_allow_entry,
)

__all__ = [
    "AllowList",
    "EgressProxyServer",
    "ProxyConfig",
    "parse_allow_entry",
]
