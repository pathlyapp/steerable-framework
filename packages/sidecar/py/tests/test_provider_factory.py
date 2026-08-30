"""default_llm_provider_factory regression: construction must not raise.

The A4 live canary caught ``OpenAICompatProvider.__init__() missing 'name'`` —
the factory had never successfully built a real provider because every prior
test injected a scripted one. Construction is local (no network), so these
tests run unconditionally.
"""

from __future__ import annotations

import pytest
from steerable_agent_runtime import CalibratingProvider, RecordingProvider
from steerable_sidecar import sidecar as sidecar_module
from steerable_sidecar.sidecar import default_llm_provider_factory


@pytest.fixture(autouse=True)
def _calibration_off(monkeypatch: pytest.MonkeyPatch):
    """Keep these construction tests hermetic: no reads of the developer's
    real ~/.steerable/token-calibration.json, no global factor registration,
    no shared-singleton leakage across tests."""
    monkeypatch.setenv("STEERABLE_TOKEN_CALIBRATION", "0")
    monkeypatch.delenv("STEERABLE_REQUEST_RECORD_PATH", raising=False)
    monkeypatch.setattr(sidecar_module, "_shared_calibration", None)
    monkeypatch.setattr(sidecar_module, "_shared_calibration_path", None)


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


def test_ollama_native_root_gets_v1_suffix() -> None:
    """Desktop canary regression: the desktop stores the native daemon root
    (http://127.0.0.1:11434) for its /api/chat client; the sidecar's
    OpenAI-compat mapping must append /v1 or every request 404s."""
    provider = default_llm_provider_factory(
        {"provider": "ollama", "model": "m", "baseUrl": "http://127.0.0.1:11434"}
    )
    assert provider.base_url == "http://127.0.0.1:11434/v1"


def test_ollama_trailing_slash_and_default() -> None:
    provider = default_llm_provider_factory(
        {"provider": "ollama", "model": "m", "baseUrl": "http://127.0.0.1:11434/"}
    )
    assert provider.base_url == "http://127.0.0.1:11434/v1"
    defaulted = default_llm_provider_factory({"provider": "ollama", "model": "m"})
    assert defaulted.base_url == "http://127.0.0.1:11434/v1"


def test_anthropic_constructs() -> None:
    provider = default_llm_provider_factory(
        {"provider": "anthropic", "model": "claude-sonnet-4-5", "apiKey": "k"}
    )
    assert provider.model == "claude-sonnet-4-5"


def test_unknown_provider_rejected() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        default_llm_provider_factory({"provider": "nope", "model": "m"})


def test_compat_auto_detected_from_base_url_host() -> None:
    """The deepseek registry entry is selected by base-URL host — the
    provider kinds are generic, so the host is the vendor signal."""
    provider = default_llm_provider_factory(
        {
            "provider": "openai_compat",
            "model": "deepseek-reasoner",
            "baseUrl": "https://api.deepseek.com/v1",
        }
    )
    assert provider.compat is not None
    assert "reasoning_content" in provider.compat.reasoning_delta_fields
    assert "prompt_cache_hit_tokens" in provider.compat.cached_tokens_fields


def test_openrouter_compat_omits_stream_options() -> None:
    """GLM endpoints do not advertise stream_options; Harbor streams."""
    from steerable_agent_runtime.llm import LLMMessage

    provider = default_llm_provider_factory(
        {
            "provider": "openai_compat",
            "model": "z-ai/glm-5.3-flash",
            "baseUrl": "https://openrouter.ai/api/v1",
        }
    )
    assert provider.compat is not None
    assert provider.compat.supports_usage_in_streaming is False
    body = provider._build_body(
        messages=[LLMMessage.text_of("user", "hi")],
        tools=None,
        temperature=None,
        max_tokens=None,
        stream=True,
        extra={},
    )
    assert "stream_options" not in body


def test_compat_explicit_param_wins_over_auto_detection() -> None:
    provider = default_llm_provider_factory(
        {
            "provider": "openai_compat",
            "model": "m",
            "baseUrl": "https://api.deepseek.com/v1",
            "compat": {"supportsUsageInStreaming": False},
        }
    )
    assert provider.compat is not None
    assert provider.compat.supports_usage_in_streaming is False
    # Fields not in the payload keep reference defaults, not the registry's.
    assert provider.compat.max_tokens_field == "max_tokens"


def test_compat_unknown_key_fails_loud() -> None:
    with pytest.raises(ValueError, match="unknown"):
        default_llm_provider_factory(
            {
                "provider": "openai_compat",
                "model": "m",
                "baseUrl": "http://x/v1",
                "compat": {"supportsEverything": True},
            }
        )


def test_model_required() -> None:
    with pytest.raises(ValueError, match="model is required"):
        default_llm_provider_factory({"provider": "ollama"})


def test_calibration_wrapper_default_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("STEERABLE_TOKEN_CALIBRATION")
    monkeypatch.setenv("STEERABLE_TOKEN_CALIBRATION_PATH", str(tmp_path / "cal.json"))
    provider = default_llm_provider_factory({"provider": "ollama", "model": "m"})
    assert isinstance(provider, CalibratingProvider)
    assert provider.inner.name == "ollama"  # type: ignore[attr-defined]
    # transparent attribute delegation still exposes the inner provider
    assert provider.base_url == "http://127.0.0.1:11434/v1"  # type: ignore[attr-defined]


def test_calibration_wrapper_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEERABLE_TOKEN_CALIBRATION", "0")
    provider = default_llm_provider_factory({"provider": "ollama", "model": "m"})
    assert not isinstance(provider, CalibratingProvider)


def test_recording_wrapper_default_off() -> None:
    provider = default_llm_provider_factory({"provider": "ollama", "model": "m"})
    assert not isinstance(provider, RecordingProvider)


def test_cache_control_wrapper_default_on_for_anthropic() -> None:
    from steerable_agent_runtime import CacheControlProvider

    provider = default_llm_provider_factory(
        {"provider": "anthropic", "model": "claude-sonnet-4-5", "apiKey": "k"}
    )
    assert isinstance(provider, CacheControlProvider)
    assert provider.inner.name == "anthropic"  # type: ignore[attr-defined]


def test_cache_control_wrapper_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    from steerable_agent_runtime import CacheControlProvider

    monkeypatch.setenv("STEERABLE_CACHE_CONTROL", "0")
    provider = default_llm_provider_factory(
        {"provider": "anthropic", "model": "claude-sonnet-4-5", "apiKey": "k"}
    )
    assert not isinstance(provider, CacheControlProvider)


def test_cache_control_wrapper_not_applied_to_openai_compat() -> None:
    # Implicit prefix caches have no breakpoint surface — no wrapper.
    from steerable_agent_runtime import CacheControlProvider

    provider = default_llm_provider_factory({"provider": "ollama", "model": "m"})
    assert not isinstance(provider, CacheControlProvider)


def test_recording_wrapper_opt_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("STEERABLE_REQUEST_RECORD_PATH", str(tmp_path / "req.jsonl"))
    provider = default_llm_provider_factory({"provider": "ollama", "model": "m"})
    assert isinstance(provider, RecordingProvider)
    # transparent attribute delegation still exposes the inner provider
    assert provider.base_url == "http://127.0.0.1:11434/v1"  # type: ignore[attr-defined]


def test_calibration_shared_across_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The factory runs per chat-stream request; samples must accumulate in a
    process-level singleton or a short turn never reaches the persist
    threshold."""
    monkeypatch.delenv("STEERABLE_TOKEN_CALIBRATION")
    monkeypatch.setenv("STEERABLE_TOKEN_CALIBRATION_PATH", str(tmp_path / "cal.json"))
    first = default_llm_provider_factory({"provider": "ollama", "model": "m"})
    second = default_llm_provider_factory({"provider": "ollama", "model": "m"})
    assert isinstance(first, CalibratingProvider)
    assert isinstance(second, CalibratingProvider)
    assert first.calibration is second.calibration


def test_calibration_flush_writes_shared_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("STEERABLE_TOKEN_CALIBRATION")
    path = tmp_path / "cal.json"
    monkeypatch.setenv("STEERABLE_TOKEN_CALIBRATION_PATH", str(path))
    provider = default_llm_provider_factory({"provider": "ollama", "model": "m"})
    assert isinstance(provider, CalibratingProvider)
    provider.calibration.record("m", est_prompt=100, obs_prompt=80)
    sidecar_module._flush_shared_calibration()
    assert path.exists()
    from steerable_agent_runtime import UsageCalibration

    loaded = UsageCalibration.load(str(path), min_samples=1, auto_register=False)
    assert loaded.models["m"].requests == 1
