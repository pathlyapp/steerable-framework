"""What `PiGlmHarborAgent` writes into the trial's models.json.

Harbor is installed in CI but not on a bare checkout, so a stub stands in
for ``harbor.agents.installed.pi`` when the real package is missing. The
stub reproduces Harbor 0.22.0's ``_build_custom_models_json`` — the version
``run.harbor_version`` pins — so the assertions below describe the patch,
and the pin is what keeps the parent's shape from moving underneath it.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

_GATEWAY = "https://gateway.example/v1"
_MODEL_ID = "z-ai/glm-5.3-flash"


class _Access:
    """Stand-in for Harbor's ResolvedModelConnection."""

    def __init__(self, base_url: str | None) -> None:
        self.configured_base_url = base_url
        self.api_key = "sk-test"
        self.env = {"OPENROUTER_API_KEY": "sk-test"}


def _install_harbor_stub() -> None:
    """Register a minimal `harbor` mirroring the pinned Pi adapter."""

    class _Pi:
        def __init__(self, *, model_api: str | None = None) -> None:
            self._model_api = model_api

        def _build_custom_models_json(
            self, access: Any, model_id: str
        ) -> dict[str, Any] | None:
            if access.configured_base_url is None:
                return None
            return {
                "providers": {
                    "harbor-endpoint": {
                        "baseUrl": access.configured_base_url,
                        "apiKey": "$OPENROUTER_API_KEY",
                        "api": self._model_api,
                        "models": [{"id": model_id}],
                    }
                }
            }

    for name in ("harbor", "harbor.agents", "harbor.agents.installed"):
        sys.modules.setdefault(name, types.ModuleType(name))
    pi_mod = types.ModuleType("harbor.agents.installed.pi")
    pi_mod.Pi = _Pi  # type: ignore[attr-defined]
    sys.modules["harbor.agents.installed.pi"] = pi_mod
    conn_mod = types.ModuleType("harbor.agents.model_connection")
    conn_mod.ResolvedModelConnection = _Access  # type: ignore[attr-defined]
    sys.modules["harbor.agents.model_connection"] = conn_mod


try:  # Real Harbor in CI exercises the actual parent implementation.
    import harbor.agents.installed.pi  # noqa: F401
except ImportError:
    _install_harbor_stub()

from evals.harbor_pi_glm import PiGlmHarborAgent  # noqa: E402


@pytest.fixture
def model() -> dict[str, Any]:
    agent = PiGlmHarborAgent(model_api="openai-completions")
    models_json = agent._build_custom_models_json(_Access(_GATEWAY), _MODEL_ID)
    assert models_json is not None
    provider = next(iter(models_json["providers"].values()))
    assert len(provider["models"]) == 1
    return provider["models"][0]


def test_agent_name_separates_the_two_pi_legs() -> None:
    """A result.json labelled "pi" cannot say whether it ran Claude or GLM."""
    assert PiGlmHarborAgent.name() == "pi-glm"


def test_output_cap_matches_the_steerable_leg(model: dict[str, Any]) -> None:
    """STEERABLE_MAX_TOKENS is 65536. Pi's 16384 default truncated GLM
    mid-reasoning: catalog 33587641909 logged `"reasoning": 16314` against
    the cap on a trial that then made no tool call."""
    assert model["maxTokens"] == 65_536


def test_context_window_is_glms_own(model: dict[str, Any]) -> None:
    """`steerable_agent_runtime.model_info` records 1048576 for z-ai/glm.
    Pi's 128000 default both compacts early and clamps maxTokens down once
    a prompt passes ~58k tokens."""
    assert model["contextWindow"] == 1_048_576


def test_reasoning_is_declared_so_thinking_reaches_the_request(
    model: dict[str, Any],
) -> None:
    """Pi reports ["off"] as the only supported thinking level when
    `reasoning` is false, clamps --thinking to off, and sends no effort at
    all — so `thinking: xhigh` in suite.yaml would be silently inert."""
    assert model["reasoning"] is True
    assert model["thinkingLevelMap"]["xhigh"] == "max"


def test_effort_travels_as_reasoning_effort(model: dict[str, Any]) -> None:
    """The steerable leg sends `reasoning_effort: max`. Pi emits that field
    only under the "openai" thinking format with reasoning effort declared
    supported; the "openrouter" format would send `reasoning: {effort}`
    instead and stop being the same request."""
    assert model["compat"]["thinkingFormat"] == "openai"
    assert model["compat"]["supportsReasoningEffort"] is True


def test_output_cap_uses_the_field_zai_honours(model: dict[str, Any]) -> None:
    """Pi's autodetect picks `max_completion_tokens` for a hostname it does
    not recognise as OpenRouter or Z.AI."""
    assert model["compat"]["maxTokensField"] == "max_tokens"


def test_route_is_pinned_to_zai(model: dict[str, Any]) -> None:
    """STEERABLE_OPENROUTER_PROVIDER / _ALLOW_FALLBACKS. OpenRouter's
    cheapest GLM route is not Z.AI, and the published TB score is Z.AI's
    endpoint, so an unpinned route compares against a different serving
    stack."""
    assert model["compat"]["openRouterRouting"] == {
        "order": ["z-ai"],
        "allow_fallbacks": False,
    }


def test_model_id_survives_the_patch(model: dict[str, Any]) -> None:
    assert model["id"] == _MODEL_ID


def test_no_endpoint_means_no_generated_model() -> None:
    """Without a configured base URL there is no custom provider entry to
    correct, and Pi falls back to its own catalog."""
    agent = PiGlmHarborAgent()
    assert agent._build_custom_models_json(_Access(None), _MODEL_ID) is None
