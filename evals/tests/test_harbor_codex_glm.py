"""What `CodexGlmHarborAgent` changes about a Codex trial.

Harbor is installed in CI but not on a bare checkout, so a stub stands in
for ``harbor.agents.installed.codex`` when the real package is missing. The
stub reproduces Harbor 0.22.0's ``_build_effective_config`` — the version
``run.harbor_version`` pins — so the assertions below describe the patch,
and the pin is what keeps the parent's merge from moving underneath it.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

_GATEWAY = "https://gateway.example/v1"
_MODEL_ID = "z-ai/glm-5.3-flash"
#: The shape `Codex.run` assembles, trimmed to the flags this leg touches.
_COMMAND = (
    "codex exec --dangerously-bypass-approvals-and-sandbox "
    "--skip-git-repo-check --model glm-5.3-flash --json "
    "--enable unified_exec -- 'do the task'"
)


def _install_harbor_stub() -> None:
    """Register a minimal `harbor` mirroring the pinned Codex adapter."""

    class _Codex:
        def __init__(self, **_: Any) -> None:
            self.model_name: str | None = None
            self._base_config: dict[str, Any] = {}

        def _build_effective_config(
            self, openai_base_url: str | None = None
        ) -> dict[str, Any]:
            config = dict(self._base_config)
            if openai_base_url:
                config["openai_base_url"] = openai_base_url
            return config

    for name in ("harbor", "harbor.agents", "harbor.agents.installed"):
        sys.modules.setdefault(name, types.ModuleType(name))
    codex_mod = types.ModuleType("harbor.agents.installed.codex")
    codex_mod.Codex = _Codex  # type: ignore[attr-defined]
    sys.modules["harbor.agents.installed.codex"] = codex_mod


try:  # Real Harbor in CI exercises the actual parent implementation.
    import harbor.agents.installed.codex  # noqa: F401
except ImportError:
    _install_harbor_stub()

from evals.harbor_codex_glm import (  # noqa: E402
    CodexGlmHarborAgent,
    restore_model_slug,
)


@pytest.fixture
def config() -> dict[str, Any]:
    agent = CodexGlmHarborAgent.__new__(CodexGlmHarborAgent)
    agent._base_config = {}
    return agent._build_effective_config(_GATEWAY)


def test_agent_name_separates_the_two_codex_legs() -> None:
    """A result.json labelled "codex" cannot say whether it ran GPT or GLM."""
    assert CodexGlmHarborAgent.name() == "codex-glm"


def test_context_window_is_glms_own(config: dict[str, Any]) -> None:
    """`steerable_agent_runtime.model_info` records 1048576 for z-ai/glm.
    Codex ships no metadata for the model, so without this key it sizes the
    window from its own default and compacts on a limit GLM does not have."""
    assert config["model_context_window"] == 1_048_576


def test_effort_matches_the_steerable_leg(config: dict[str, Any]) -> None:
    """STEERABLE_REASONING_EFFORT sends `max`, and Codex's ReasoningEffort
    enum carries it, so effort is not one of the differences measured. Left
    unset, Harbor's CliFlag default puts `high` on the command line."""
    assert config["model_reasoning_effort"] == "max"


def test_the_gateway_still_reaches_config_toml(config: dict[str, Any]) -> None:
    """Codex 0.118.0 onward honours `openai_base_url` from config.toml and
    not the env var, so dropping the parent's merge would send every request
    to api.openai.com."""
    assert config["openai_base_url"] == _GATEWAY


def test_a_supplied_config_wins_over_these_defaults() -> None:
    """`--agent-kwarg config=...` exists to vary exactly these fields; a
    leg that could not override them could not be A/B tested."""
    agent = CodexGlmHarborAgent.__new__(CodexGlmHarborAgent)
    agent._base_config = {"model_context_window": 200_000, "model_reasoning_effort": "low"}
    config = agent._build_effective_config(_GATEWAY)
    assert config["model_context_window"] == 200_000
    assert config["model_reasoning_effort"] == "low"


def test_the_full_gateway_slug_reaches_the_command() -> None:
    """Harbor sends `--model glm-5.3-flash`; a gateway keyed by vendor/model
    answers that with an unknown-model error on every trial."""
    assert f"--model {_MODEL_ID} " in restore_model_slug(_COMMAND, _MODEL_ID)


def test_only_the_model_flag_changes() -> None:
    """The rewrite is a correction, not a rebuild: everything else Harbor
    put on the command has to survive it."""
    restored = restore_model_slug(_COMMAND, _MODEL_ID)
    assert restored == _COMMAND.replace(
        "--model glm-5.3-flash ", f"--model {_MODEL_ID} ", 1
    )


def test_setup_commands_pass_through_untouched() -> None:
    """`run` calls exec_as_agent several times before the agent itself, and
    those commands carry no model flag to correct."""
    setup = 'mkdir -p "$CODEX_HOME" /tmp/codex-secrets'
    assert restore_model_slug(setup, _MODEL_ID) == setup


def test_a_harbor_that_stops_truncating_fails_loudly() -> None:
    """The alternative is a leg that keeps scoring while measuring whatever
    model the new parent chose, which is how the first pi-glm catalog run
    produced 18/54 and read as a harness verdict."""
    already_full = _COMMAND.replace("--model glm-5.3-flash", f"--model {_MODEL_ID}")
    with pytest.raises(ValueError, match="no longer truncates"):
        restore_model_slug(already_full, _MODEL_ID)
