"""What `ClaudeCodeGlmHarborAgent` guarantees about a Claude Code trial.

Harbor is installed in CI but not on a bare checkout, so a stub stands in
for ``harbor.agents.installed.claude_code`` when the real package is
missing. The stub reproduces Harbor 0.22.0's ``_resolved_model_name`` — the
version ``run.harbor_version`` pins — because the whole point of this leg's
override is the branch that method takes when no base URL is configured.
"""

from __future__ import annotations

import sys
import types

import pytest

_GATEWAY = "https://gateway.example/v1"
_MODEL_ID = "z-ai/glm-5.3-flash"


class _Access:
    """Stand-in for Harbor's ResolvedModelConnection."""

    def __init__(self, base_url: str | None) -> None:
        self.provider = "anthropic"
        self.configured_base_url = base_url


def _install_harbor_stub() -> None:
    """Register a minimal `harbor` mirroring the pinned Claude Code adapter."""

    class _ClaudeCode:
        model_name: str | None = None

        def _resolved_model_name(self) -> str | None:
            access = self.model_connection  # type: ignore[attr-defined]
            if self.model_name:
                if access.provider is None:
                    return self.model_name.split("/", 1)[-1]
                if access.configured_base_url:
                    return self.model_name
                return self.model_name.split("/")[-1]
            return None

    for name in ("harbor", "harbor.agents", "harbor.agents.installed"):
        sys.modules.setdefault(name, types.ModuleType(name))
    mod = types.ModuleType("harbor.agents.installed.claude_code")
    mod.ClaudeCode = _ClaudeCode  # type: ignore[attr-defined]
    sys.modules["harbor.agents.installed.claude_code"] = mod


try:  # Real Harbor in CI exercises the actual parent implementation.
    import harbor.agents.installed.claude_code  # noqa: F401
except ImportError:
    _install_harbor_stub()

from evals.harbor_claude_code_glm import ClaudeCodeGlmHarborAgent  # noqa: E402


class _Leg(ClaudeCodeGlmHarborAgent):
    """The agent with its connection pinned, so no trial has to be built."""

    def __init__(self, base_url: str | None) -> None:
        self.model_name = _MODEL_ID
        self._access = _Access(base_url)

    @property
    def model_connection(self) -> _Access:  # type: ignore[override]
        return self._access


def test_agent_name_separates_the_two_claude_code_legs() -> None:
    """A result.json labelled "claude-code" cannot say whether it ran Sonnet
    or GLM."""
    assert ClaudeCodeGlmHarborAgent.name() == "claude-code-glm"


def test_the_full_gateway_slug_reaches_the_cli() -> None:
    """With a base URL configured the parent forwards the model name
    untouched, which is why this leg's model carries no provider prefix."""
    assert _Leg(_GATEWAY)._resolved_model_name() == _MODEL_ID


def test_a_missing_base_url_fails_instead_of_reaching_anthropic() -> None:
    """Left to the parent, no base URL means `z-ai/glm-5.3-flash` is cut to
    `glm-5.3-flash` and sent to api.anthropic.com, whose unknown-model error
    arrives per trial and reads as a task failure rather than a leg that was
    never pointed at the gateway."""
    with pytest.raises(ValueError, match="ANTHROPIC_BASE_URL"):
        _Leg(None)._resolved_model_name()
