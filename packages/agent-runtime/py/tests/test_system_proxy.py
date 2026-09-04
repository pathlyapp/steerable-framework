"""Provider endpoints must reach a local runtime under a host's proxy settings.

A macOS system proxy captures loopback traffic that the OS declares direct,
because `urllib.request.getproxies()` returns the proxy hosts without the
ExceptionsList that qualifies them. Left uncorrected, a local Ollama endpoint
answers with the proxy's HTTP 502 instead of the model's reply.

The environment-variable form fails earlier and harder: httpx builds a transport
per declared proxy while constructing the client, so `all_proxy=socks5://…`
without the `socks` extra raises before any URL is matched. These tests fix which
endpoints that error may reach and which keep their declared proxy.
"""

from __future__ import annotations

import pytest
from steerable_agent_runtime.llm.system_proxy import client_env_kwargs, direct_mounts


def _bypass(*hosts: str):
    return lambda host: host in hosts


SOCKS_ENV = {"http": "http://127.0.0.1:7890", "all": "socks5://127.0.0.1:7891"}


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


def test_direct_mounts_defers_the_environment_case() -> None:
    """`direct_mounts` answers only for the System Configuration reader.

    Environment-declared proxies need the confinement marker and the platform's
    own exception list to decide, which is `client_env_kwargs`' job.
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


def test_exempt_endpoint_survives_a_socks_proxy_httpx_cannot_build() -> None:
    """`all_proxy=socks5://…` without socksio must not fail a NO_PROXY'd endpoint.

    httpx builds transports at construction, so the unbuildable scheme raises
    before `NO_PROXY` can exempt the loopback endpoint it never applied to.
    """
    assert client_env_kwargs(
        "http://127.0.0.1:11434/v1",
        env_proxies=lambda: SOCKS_ENV,
        env_bypass=_bypass("127.0.0.1"),
        platform_list_bypass=_bypass(),
        has_module=lambda name: False,
    ) == {"trust_env": False}


def test_platform_exemption_covers_the_snippet_that_omits_no_proxy() -> None:
    """`export …all_proxy=socks5://…` alone leaves the platform list as the only bypass."""
    assert client_env_kwargs(
        "http://127.0.0.1:11434/v1",
        env_proxies=lambda: SOCKS_ENV,
        env_bypass=_bypass(),
        platform_list_bypass=_bypass("127.0.0.1"),
        has_module=lambda name: False,
    ) == {"trust_env": False}


def test_confined_egress_owns_even_a_platform_exempt_host() -> None:
    """The marker, not the host, is what forbids the exemption.

    Same loopback endpoint and same platform verdict as the ambient case below;
    only `STEERABLE_EGRESS_CONFINED` differs, and the proxy keeps the host.
    """
    assert (
        client_env_kwargs(
            "http://127.0.0.1:11434/v1",
            env_proxies=lambda: {"https": "http://127.0.0.1:8080"},
            env_bypass=_bypass(),
            platform_list_bypass=_bypass("127.0.0.1"),
            confined=lambda: True,
            has_module=lambda name: True,
        )
        == {}
    )


def test_proxied_endpoint_keeps_the_unbuildable_proxy_error() -> None:
    """Going direct here would be the silent proxy bypass the declaration forbids."""
    assert (
        client_env_kwargs(
            "https://api.openai.com/v1",
            env_proxies=lambda: SOCKS_ENV,
            env_bypass=_bypass("127.0.0.1"),
            platform_list_bypass=_bypass("127.0.0.1"),
            has_module=lambda name: False,
        )
        == {}
    )


def test_ambient_proxy_does_not_capture_a_platform_exempt_endpoint() -> None:
    """Installing the socks extra makes the scheme buildable, not the proxy correct.

    With socksio present the proxy map builds, and httpx would ask the proxy for
    the loopback endpoint — the HTTP 502 this module exists to prevent, arriving
    through the environment instead of System Configuration.
    """
    assert client_env_kwargs(
        "http://127.0.0.1:11434/v1",
        env_proxies=lambda: SOCKS_ENV,
        env_bypass=_bypass(),
        platform_list_bypass=_bypass("127.0.0.1"),
        confined=lambda: False,
        has_module=lambda name: True,
    ) == {"mounts": {"all://127.0.0.1": None}}


def test_buildable_proxy_pins_the_host_instead_of_dropping_trust_env() -> None:
    """`trust_env=False` costs netrc and SSL env, so a working pin is preferred."""
    assert client_env_kwargs(
        "http://127.0.0.1:11434/v1",
        env_proxies=lambda: {"http": "http://127.0.0.1:7890"},
        env_bypass=_bypass("127.0.0.1"),
        platform_list_bypass=_bypass("127.0.0.1"),
        confined=lambda: False,
        has_module=lambda name: False,
    ) == {"mounts": {"all://127.0.0.1": None}}


def test_platform_bypass_correction_is_reported_as_mounts() -> None:
    assert client_env_kwargs(
        "http://127.0.0.1:11434/v1",
        env_proxies=dict,
        platform_bypass=_bypass("127.0.0.1"),
    ) == {"mounts": {"all://127.0.0.1": None}}


@pytest.mark.parametrize("has_socks", [True, False])
def test_kwargs_keep_an_exempt_endpoint_off_the_proxy_transport(has_socks: bool) -> None:
    """Under the real proxy variables, both answers reach httpx's own transport.

    `mounts` and `trust_env=False` are different escapes from the same capture, so
    each has to survive contact with httpx rather than merely typecheck.
    """
    import httpx

    url = httpx.URL("http://127.0.0.1:11434/v1/chat/completions")
    kwargs = client_env_kwargs(
        "http://127.0.0.1:11434/v1",
        env_proxies=lambda: SOCKS_ENV,
        env_bypass=_bypass(),
        platform_list_bypass=_bypass("127.0.0.1"),
        confined=lambda: False,
        has_module=lambda name: has_socks,
    )
    with pytest.MonkeyPatch.context() as mp:
        for key, value in (("all_proxy", SOCKS_ENV["all"]), ("http_proxy", SOCKS_ENV["http"])):
            mp.setenv(key, value)
        client = httpx.AsyncClient(base_url="http://127.0.0.1:11434/v1", **kwargs)
        proxied = httpx.AsyncClient(base_url="http://127.0.0.1:11434/v1", trust_env=True)
    assert client._transport_for_url(url) is client._transport
    assert proxied._transport_for_url(url) is not proxied._transport


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
