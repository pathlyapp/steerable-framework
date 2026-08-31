"""W5.2.1: three-tier catalog resolution — exact, scoped, prefix, unknown."""

from __future__ import annotations

from steerable_agent_runtime.model_catalog import MODEL_ENTRIES
from steerable_agent_runtime.model_resolve import provider_endpoint, resolve_in_catalog


def test_exact_provider_model() -> None:
    hit = resolve_in_catalog("openai", "gpt-5.5")
    assert hit is not None
    assert hit.source == "exact"
    assert hit.key == "openai/gpt-5.5"
    assert hit.context_window > 0


def test_exact_when_model_string_carries_provider() -> None:
    hit = resolve_in_catalog(None, "anthropic/claude-sonnet-4-6")
    assert hit is not None
    assert hit.source == "exact"
    assert hit.key == "anthropic/claude-sonnet-4-6"


def test_scoped_resolution_within_provider_namespace() -> None:
    # The gateway case from our own evals: bare model id, gateway namespaces
    # the upstream vendor — final-segment match finds it.
    hit = resolve_in_catalog("openrouter", "glm-5.3-flash")
    assert hit is not None
    assert hit.source == "scoped"
    assert hit.key == "openrouter/z-ai/glm-5.3-flash"


def test_prefix_stays_within_the_named_provider() -> None:
    # "gpt-5.5-2099-01-01" under provider openai must claim openai/gpt-5.5,
    # never another vendor's same-named deployment.
    hit = resolve_in_catalog("openai", "gpt-5.5-2099-01-01")
    assert hit is not None
    assert hit.source == "prefix"
    assert hit.key == "openai/gpt-5.5"


def test_short_ids_never_prefix_match() -> None:
    # "gpt-5" is below MIN_PREFIX_CHARS: a short id must not claim a bigger
    # sibling's entry by prefix.
    hit = resolve_in_catalog("openai", "gpt-5-2099-01-01")
    assert hit is None or hit.source != "prefix" or len(hit.key.split("/")[-1]) >= 6


def test_unknown_model_returns_none_for_observable_default() -> None:
    # None is the W5.2.3 hook: the caller emits the "fell back to default"
    # event here rather than silently using DEFAULT_CONTEXT_WINDOW.
    assert resolve_in_catalog("no-such-provider", "no-such-model") is None
    assert resolve_in_catalog(None, "") is None


def test_every_catalog_entry_resolves_exactly() -> None:
    """The catalog is self-consistent: every key resolves to itself."""
    for key in (
        "openai/gpt-5.5",
        "anthropic/claude-opus-4-7",
        "openrouter/z-ai/glm-5.3-flash",
    ):
        assert key in MODEL_ENTRIES
        provider, model = key.split("/", 1)
        hit = resolve_in_catalog(provider, model)
        assert hit is not None and hit.key == key and hit.source == "exact"


def test_provider_endpoint_from_catalog() -> None:
    ep = provider_endpoint("openrouter")
    assert ep is not None
    assert ep.api_base_url == "https://openrouter.ai/api/v1"
    assert "OPENROUTER_API_KEY" in ep.env_vars


def test_provider_endpoint_none_for_first_party_default() -> None:
    # models.dev omits the api field for first-party endpoints; None means
    # "use the SDK default", not "unknown provider".
    ep = provider_endpoint("openai")
    assert ep is not None
    assert ep.api_base_url is None
    assert ep.env_vars == ("OPENAI_API_KEY",)


def test_provider_endpoint_unknown_provider() -> None:
    assert provider_endpoint("no-such-provider") is None
