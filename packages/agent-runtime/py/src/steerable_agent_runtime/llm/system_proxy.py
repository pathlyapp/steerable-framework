"""Restore the platform's proxy bypass list for provider endpoints.

`httpx(trust_env=True)` derives its proxy map from `urllib.request.getproxies()`.
On macOS that reader returns the System Configuration proxy hosts but drops the
accompanying ExceptionsList, so a system proxy also captures the traffic the OS
itself declares direct. Pointing a provider at a local runtime — Ollama on
`http://127.0.0.1:11434/v1`, vLLM on a LAN address — then yields the proxy's
answer (commonly HTTP 502, since proxies refuse loopback targets) instead of the
model's, even though macOS lists `127.0.0.1` as an exception.

Explicit `*_PROXY` environment variables are left alone. They carry their own
`NO_PROXY` semantics, which httpx already honors, and an operator who sets them
— the desktop's egress broker among them — is naming the egress point
deliberately. Exempting hosts from that declaration would widen confinement
beyond what was asked for.

The bypass decision comes from `urllib.request.proxy_bypass`, which implements
each platform's own matching rules (macOS ExceptionsList with CIDR and wildcard
entries, the Windows registry, `no_proxy` elsewhere). Reading it costs a System
Configuration round trip, so results are memoized per host: a proxy change mid
-session takes effect on the next sidecar boot.
"""

from __future__ import annotations

import urllib.request
from collections.abc import Callable, Mapping
from functools import lru_cache
from urllib.parse import urlsplit


@lru_cache(maxsize=128)
def _platform_says_direct(host: str) -> bool:
    try:
        return bool(urllib.request.proxy_bypass(host))
    except Exception:
        # Reading proxy configuration must never fail a request: an
        # unavailable `_scproxy`, a registry error, or a malformed
        # `no_proxy` all mean "no opinion", which is httpx's own default.
        return False


def direct_mounts(
    base_url: str,
    *,
    env_proxies: Callable[[], Mapping[str, str]] = urllib.request.getproxies_environment,
    platform_bypass: Callable[[str], bool] = _platform_says_direct,
) -> dict[str, None] | None:
    """httpx `mounts` pinning `base_url` to a direct connection, or None.

    None means "no override": either the environment declares the proxy, or the
    platform has no bypass opinion about this host.
    """
    if env_proxies():
        return None
    host = urlsplit(base_url).hostname
    if not host or not platform_bypass(host):
        return None
    pattern = f"[{host}]" if ":" in host else host
    return {f"all://{pattern}": None}
