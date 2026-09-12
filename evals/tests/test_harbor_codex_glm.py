"""What `CodexGlmHarborAgent` guarantees about a Codex CLI trial.

Harbor is installed in CI but not on a bare checkout, so a stub stands in
for ``harbor.agents.installed.codex`` when the real package is missing. The
stub reproduces Harbor 0.22.0's ``split("/")[-1]`` model truncation — the
version ``run.harbor_version`` pins — because restoring the nested gateway
id is the whole point of this leg.
"""

from __future__ import annotations

import sys
import types

import pytest

_GATEWAY = "https://gateway.example/v1"
_MODEL_ID = "openai/z-ai/glm-5.3-flash"


class _Access:
    """Stand-in for Harbor's ResolvedModelConnection."""

    def __init__(self, base_url: str | None) -> None:
        self.configured_base_url = base_url
        self.api_key = "sk-test"


def _install_harbor_stub() -> None:
    """Register a minimal `harbor` mirroring the pinned Codex adapter."""

    class _Codex:
        model_name: str | None = None

        async def exec_as_agent(self, environment, command, **kwargs):
            return command

        async def run(self, instruction, environment, context) -> None:
            return None

    for name in (
        "harbor",
        "harbor.agents",
        "harbor.agents.installed",
        "harbor.environments",
        "harbor.environments.base",
        "harbor.models",
        "harbor.models.agent",
        "harbor.models.agent.context",
    ):
        sys.modules.setdefault(name, types.ModuleType(name))
    mod = types.ModuleType("harbor.agents.installed.codex")
    mod.Codex = _Codex  # type: ignore[attr-defined]
    sys.modules["harbor.agents.installed.codex"] = mod
    env_mod = sys.modules["harbor.environments.base"]
    env_mod.BaseEnvironment = object  # type: ignore[attr-defined]
    ctx_mod = sys.modules["harbor.models.agent.context"]
    ctx_mod.AgentContext = object  # type: ignore[attr-defined]


try:
    import harbor.agents.installed.codex  # noqa: F401
except ImportError:
    _install_harbor_stub()

from evals.harbor_codex_glm import (  # noqa: E402
    CodexGlmHarborAgent,
    gateway_codex_model,
    rewrite_codex_exec_model,
)


class _Leg(CodexGlmHarborAgent):
    """The agent with its connection pinned, so no trial has to be built."""

    def __init__(self, base_url: str | None) -> None:
        self.model_name = _MODEL_ID
        self._access = _Access(base_url)

    @property
    def model_connection(self) -> _Access:  # type: ignore[override]
        return self._access


def test_agent_name_separates_the_two_codex_legs() -> None:
    """A result.json labelled "codex" cannot say whether it ran gpt-5.5
    or the gateway GLM cell."""
    assert CodexGlmHarborAgent.name() == "codex-glm"


def test_harbor_prefix_is_stripped_nested_id_survives() -> None:
    assert gateway_codex_model("openai/z-ai/glm-5.3-flash") == "z-ai/glm-5.3-flash"
    assert (
        gateway_codex_model("openai/deepseek/deepseek-v4-flash-0731")
        == "deepseek/deepseek-v4-flash-0731"
    )
    assert gateway_codex_model("z-ai/glm-5.3-flash") == "z-ai/glm-5.3-flash"


def test_exec_restores_the_nested_id_harbor_truncated() -> None:
    command = (
        "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
        "codex exec --dangerously-bypass-approvals-and-sandbox "
        "--skip-git-repo-check --model glm-5.3-flash --json -- "
        "'solve the task' "
    )
    rewritten = rewrite_codex_exec_model(command, _MODEL_ID)
    assert "--model z-ai/glm-5.3-flash " in rewritten
    assert "--model glm-5.3-flash " not in rewritten


def test_mkdir_is_not_rewritten() -> None:
    command = 'mkdir -p "$CODEX_HOME" /tmp/codex-secrets'
    assert rewrite_codex_exec_model(command, _MODEL_ID) == command


def test_a_missing_base_url_fails_instead_of_reaching_openai() -> None:
    with pytest.raises(ValueError, match="OPENAI_BASE_URL"):
        _Leg(None)._require_gateway()


def test_gateway_model_is_the_openrouter_slug() -> None:
    assert _Leg(_GATEWAY)._require_gateway() == "z-ai/glm-5.3-flash"
