"""Provider endpoints must honor the platform's proxy bypass list.

A macOS system proxy captures loopback traffic that the OS declares direct,
because `urllib.request.getproxies()` returns the proxy hosts without the
ExceptionsList that qualifies them. Left uncorrected, a local Ollama endpoint
answers with the proxy's HTTP 502 instead of the model's reply.
"""

from __future__ import annotations

from steerable_agent_runtime.llm.system_proxy import direct_mounts


def _bypass(*hosts: str):
    return lambda host: host in hosts


def test_bypassed_host_is_pinned_to_a_direct_connection() -> None:
    mounts = direct_mounts(
        "http://127.0.0.1:11434/v1",
        env_proxies=dict,
        platform_bypass=_bypass("127.0.0.1"),
    )
    assert mounts == {"all://127.0.0.1": None}


def test_proxied_host_keeps_httpx_defaults() -> None:
    assert (
        direct_mounts(
            "https://api.openai.com/v1",
            env_proxies=dict,
            platform_bypass=_bypass("127.0.0.1"),
        )
        is None
    )


def test_environment_proxy_declaration_is_never_overridden() -> None:
    """An operator-declared egress point owns every host, loopback included.

    The desktop's egress broker sets `HTTPS_PROXY` to confine outbound traffic;
    exempting hosts here would widen that confinement.
    """
    assert (
        direct_mounts(
            "http://127.0.0.1:11434/v1",
            env_proxies=lambda: {"http": "http://127.0.0.1:8080"},
            platform_bypass=_bypass("127.0.0.1"),
        )
        is None
    )


def test_ipv6_host_is_bracketed_for_the_mount_pattern() -> None:
    mounts = direct_mounts(
        "http://[::1]:11434/v1",
        env_proxies=dict,
        platform_bypass=_bypass("::1"),
    )
    assert mounts == {"all://[::1]": None}


def test_unparseable_base_url_yields_no_override() -> None:
    assert direct_mounts("not a url", env_proxies=dict, platform_bypass=_bypass()) is None


def test_mount_pattern_is_accepted_by_httpx() -> None:
    """The pattern is httpx's own syntax, not a string we invented."""
    import httpx

    mounts = direct_mounts(
        "http://127.0.0.1:11434/v1",
        env_proxies=dict,
        platform_bypass=_bypass("127.0.0.1"),
    )
    assert mounts is not None
    client = httpx.AsyncClient(mounts=mounts)
    transport = client._transport_for_url(httpx.URL("http://127.0.0.1:11434/v1/chat/completions"))
    assert transport is client._transport
