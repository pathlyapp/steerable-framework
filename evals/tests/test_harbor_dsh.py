"""What the DSH Harbor adapter writes into the trial's settings.yaml.

Harbor is installed in CI but not on a bare checkout, so a stub stands in
for ``harbor.agents.installed.base`` when the real package is missing. The
settings document is a pure function; the class tests only need ``name()``.
"""

from __future__ import annotations

import sys
import types

import pytest
import yaml

_GATEWAY = "https://gateway.example/v1"
_MODEL_ID = "z-ai/glm-5.3-flash"


def _install_harbor_stub() -> None:
    """Register a minimal `harbor` so the adapter module imports."""

    class _Base:
        def __init__(self, *args, **kwargs) -> None:
            pass

    def _identity(fn):
        return fn

    for name in (
        "harbor",
        "harbor.agents",
        "harbor.agents.installed",
        "harbor.agents.installed.base",
        "harbor.agents.model_connection",
        "harbor.environments",
        "harbor.environments.base",
        "harbor.models",
        "harbor.models.agent",
        "harbor.models.agent.context",
    ):
        sys.modules.setdefault(name, types.ModuleType(name))
    base = sys.modules["harbor.agents.installed.base"]
    base.BaseInstalledAgent = _Base  # type: ignore[attr-defined]
    base.with_prompt_template = _identity  # type: ignore[attr-defined]
    conn = sys.modules["harbor.agents.model_connection"]

    class _Spec:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    conn.ModelConnectionSpec = _Spec  # type: ignore[attr-defined]
    sys.modules["harbor.environments.base"].BaseEnvironment = object  # type: ignore[attr-defined]
    sys.modules["harbor.models.agent.context"].AgentContext = object  # type: ignore[attr-defined]


try:
    import harbor.agents.installed.base  # noqa: F401
except ImportError:
    _install_harbor_stub()

from evals.harbor_dsh import (  # noqa: E402
    DshHarborAgent,
    context_window_for,
    dsh_settings_yaml,
    gateway_dsh_model,
    openrouter_routing,
    resolve_api_key_env,
    resolve_base_url,
)


def test_agent_name_is_dsh() -> None:
    assert DshHarborAgent.name() == "dsh"


def test_harbor_prefix_is_stripped() -> None:
    assert gateway_dsh_model("openai/z-ai/glm-5.3-flash") == "z-ai/glm-5.3-flash"
    assert gateway_dsh_model("openrouter/qwen/qwen3.8-27b") == "qwen/qwen3.8-27b"


def test_context_window_matches_pi_glm() -> None:
    assert context_window_for("z-ai/glm-5.3-flash") == 1_048_576
    assert context_window_for("deepseek/deepseek-v4-flash-0731") == 1_048_576
    assert context_window_for("qwen/qwen3.8-27b") == 262_144


def test_settings_yaml_is_a_headless_eval_document() -> None:
    raw = dsh_settings_yaml(
        base_url=_GATEWAY,
        model_id=_MODEL_ID,
        api_key_env="OPENROUTER_API_KEY",
        effort="high",
        provider_order=["z-ai"],
        context_window=1_048_576,
    )
    parsed = yaml.safe_load(raw)
    assert parsed["agent-default-model"]["provider"] == "gateway"
    assert parsed["agent-default-model"]["model"] == _MODEL_ID
    assert parsed["agent-default-model"]["reasoningEffort"] == "high"
    assert parsed["permission"]["defaultPreset"] == "danger-full-access"
    provider = parsed["llm-pi-ai"]["providers"]["gateway"]
    assert provider["api"] == "openai-completions"
    assert provider["baseURL"] == _GATEWAY
    assert provider["apiKeyEnv"] == "OPENROUTER_API_KEY"
    assert provider["compat"]["supportsDeveloperRole"] is False
    assert provider["compat"]["maxTokensField"] == "max_tokens"
    assert provider["compat"]["openRouterRouting"] == {
        "order": ["z-ai"],
        "allow_fallbacks": False,
    }
    assert provider["models"][0]["id"] == _MODEL_ID
    assert provider["models"][0]["reasoning"] is True
    assert provider["models"][0]["maxTokens"] == 65_536


def test_key_env_prefers_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "or")
    monkeypatch.setenv("STEERABLE_API_KEY", "st")
    assert resolve_api_key_env() == "OPENROUTER_API_KEY"


def test_base_url_prefers_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_BASE_URL", _GATEWAY)
    monkeypatch.setenv("STEERABLE_BASE_URL", "https://other.example/v1")
    assert resolve_base_url() == _GATEWAY


def test_route_pin_follows_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEERABLE_OPENROUTER_PROVIDER", "alibaba")
    assert openrouter_routing("deepseek/deepseek-v4-flash-0731") == {
        "order": ["alibaba"],
        "allow_fallbacks": False,
    }
