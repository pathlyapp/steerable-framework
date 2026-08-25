"""default_llm_provider_factory regression: construction must not raise.

The A4 live canary caught ``OpenAICompatProvider.__init__() missing 'name'`` —
the factory had never successfully built a real provider because every prior
test injected a scripted one. Construction is local (no network), so these
tests run unconditionally.
"""

from __future__ import annotations

import pytest

from steerable_sidecar.sidecar import default_llm_provider_factory


def test_openai_compat_constructs() -> None:
    provider = default_llm_provider_factory(
        {"provider": "openai_compat", "model": "gpt-4o-mini", "baseUrl": "http://x/v1"}
    )
    assert provider.model == "gpt-4o-mini"
    assert provider.name == "openai_compat"


def test_ollama_alias_constructs() -> None:
    provider = default_llm_provider_factory(
        {"provider": "ollama", "model": "gpt-oss:20b-cloud", "baseUrl": "http://127.0.0.1:11434/v1"}
    )
    assert provider.name == "ollama"


def test_anthropic_constructs() -> None:
    provider = default_llm_provider_factory(
        {"provider": "anthropic", "model": "claude-sonnet-4-5", "apiKey": "k"}
    )
    assert provider.model == "claude-sonnet-4-5"


def test_unknown_provider_rejected() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        default_llm_provider_factory({"provider": "nope", "model": "m"})


def test_model_required() -> None:
    with pytest.raises(ValueError, match="model is required"):
        default_llm_provider_factory({"provider": "ollama"})
