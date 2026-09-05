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

from harbor.agents.installed.claude_code import ClaudeCode  # noqa: E402

from evals.harbor_claude_code_glm import (  # noqa: E402
    ClaudeCodeGlmHarborAgent,
    anthropic_api_root,
)


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


def _auth_env(monkeypatch: pytest.MonkeyPatch, **parent_env: str) -> dict[str, str]:
    """Run the override over exactly what the parent is pinned to contribute."""
    monkeypatch.setattr(
        ClaudeCode,
        "_resolve_auth_env",
        lambda self: dict(parent_env),
        raising=False,
    )
    return _Leg(_GATEWAY)._resolve_auth_env()


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api"),
        ("https://openrouter.ai/api/v1/", "https://openrouter.ai/api"),
        ("https://openrouter.ai/api", "https://openrouter.ai/api"),
    ],
)
def test_the_cli_receives_the_api_root_it_appends_v1_messages_to(
    configured: str, expected: str
) -> None:
    """The gateway URL the OpenAI-dialect legs share ends in `/v1`, and the
    Claude CLI appends `/v1/messages` to whatever it is given, so forwarding
    that URL verbatim requests `/api/v1/v1/messages` and draws a 404 that is
    indistinguishable from the gateway lacking the Anthropic endpoint."""
    assert anthropic_api_root(configured) == expected


def test_the_gateway_key_moves_off_the_variable_that_selects_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-empty ANTHROPIC_API_KEY makes the CLI authenticate against
    Anthropic even with the base URL pointed elsewhere, so the key has to
    arrive as ANTHROPIC_AUTH_TOKEN and the key variable has to stay empty
    rather than merely absent."""
    env = _auth_env(
        monkeypatch, ANTHROPIC_API_KEY="gateway-key", ANTHROPIC_BASE_URL=_GATEWAY
    )
    assert env["ANTHROPIC_AUTH_TOKEN"] == "gateway-key"
    assert env["ANTHROPIC_API_KEY"] == ""


def test_model_discovery_is_enabled_so_the_cli_stops_rejecting_glm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without this the CLI refuses any model id outside its built-in list
    before dialing out, and reports it as a synthetic `model_not_found` turn
    with `duration_api_ms: 0` on every trial."""
    env = _auth_env(
        monkeypatch, ANTHROPIC_API_KEY="gateway-key", ANTHROPIC_BASE_URL=_GATEWAY
    )
    assert env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"


def test_a_bedrock_style_env_without_a_base_url_is_left_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parent omits ANTHROPIC_BASE_URL on the Bedrock path, where none of
    the three OpenRouter conventions apply."""
    parent_env = {"CLAUDE_CODE_USE_BEDROCK": "1"}
    assert _auth_env(monkeypatch, **parent_env) == parent_env
