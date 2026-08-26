"""default_llm_provider_factory regression: construction must not raise.

The A4 live canary caught ``OpenAICompatProvider.__init__() missing 'name'`` —
the factory had never successfully built a real provider because every prior
test injected a scripted one. Construction is local (no network), so these
tests run unconditionally.
"""

from __future__ import annotations

import pytest

from steerable_agent_runtime import CalibratingProvider
from steerable_sidecar import sidecar as sidecar_module
from steerable_sidecar.sidecar import default_llm_provider_factory


@pytest.fixture(autouse=True)
def _calibration_off(monkeypatch: pytest.MonkeyPatch):
    """Keep these construction tests hermetic: no reads of the developer's
    real ~/.steerable/token-calibration.json, no global factor registration,
    no shared-singleton leakage across tests."""
    monkeypatch.setenv("STEERABLE_TOKEN_CALIBRATION", "0")
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
