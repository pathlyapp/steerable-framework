"""Harbor Claude Code agent pointed at the product gateway serving GLM.

Harbor's stock ``ClaudeCode`` already selects the model correctly once
``ANTHROPIC_BASE_URL`` is set: it keeps the full gateway model id, pins the
sonnet, opus, haiku, and subagent aliases to it, and unlike Pi it writes no
context-window or output-cap metadata of its own. What it does not do is
speak the three conventions OpenRouter's Anthropic endpoint requires, all
of which fail before any request leaves the container:

- ``ANTHROPIC_BASE_URL`` must be the API root, because the CLI appends
  ``/v1/messages`` itself. Passing the gateway's OpenAI-dialect base URL
  verbatim yields ``/api/v1/v1/messages``.
- the gateway key must arrive in ``ANTHROPIC_AUTH_TOKEN``, with
  ``ANTHROPIC_API_KEY`` empty. A non-empty ``ANTHROPIC_API_KEY`` makes the
  CLI authenticate against Anthropic instead of the gateway.
- gateway model discovery must be enabled, or the CLI rejects any model id
  outside its built-in list without dialing out. That rejection surfaces as
  a synthetic ``model_not_found`` turn with ``duration_api_ms: 0``, which
  reads like a gateway 404 rather than a client-side allowlist.

The base-URL guard matters for the same reason: with no configured base URL
the parent truncates ``z-ai/glm-5.3-flash`` to ``glm-5.3-flash`` and sends
it to ``api.anthropic.com``, whose unknown-model error reads like a task
failure rather than a misconfigured leg.
"""

from __future__ import annotations

try:
    from typing import override
except ImportError:  # Python < 3.12 — evals unit tests still collect.

    def override(f):  # type: ignore[misc]
        return f

from harbor.agents.installed.claude_code import ClaudeCode

_GATEWAY_MODEL_DISCOVERY = "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"


def anthropic_api_root(base_url: str) -> str:
    """Strip the OpenAI-dialect ``/v1`` suffix the Claude CLI re-appends.

    :param base_url: gateway base URL as the OpenAI-dialect legs consume it.
    :returns: the API root to hand the Claude CLI.
    """
    trimmed = base_url.rstrip("/")
    suffix = "/v1"
    return trimmed[: -len(suffix)] if trimmed.endswith(suffix) else trimmed


class ClaudeCodeGlmHarborAgent(ClaudeCode):
    """Claude Code driving the product model through the product gateway."""

    @staticmethod
    @override
    def name() -> str:
        # Distinct from "claude-code" so a result.json says which leg
        # produced it: the stock name cannot distinguish the Sonnet
        # baseline from this one.
        return "claude-code-glm"

    @override
    def _resolved_model_name(self) -> str | None:
        if not self.model_connection.configured_base_url:
            raise ValueError(
                "claude-code-glm needs ANTHROPIC_BASE_URL set to the product "
                "gateway. Without it Claude Code keeps only the last segment "
                f"of {self.model_name!r} and sends it to Anthropic."
            )
        return super()._resolved_model_name()

    @override
    def _resolve_auth_env(self) -> dict[str, str]:
        env = super()._resolve_auth_env()
        base_url = env.get("ANTHROPIC_BASE_URL")
        if base_url is None:
            return env
        env["ANTHROPIC_BASE_URL"] = anthropic_api_root(base_url)
        # The parent drops empty values, so re-add ANTHROPIC_API_KEY after it
        # has handed the key to ANTHROPIC_AUTH_TOKEN.
        env["ANTHROPIC_AUTH_TOKEN"] = env.pop("ANTHROPIC_API_KEY", "")
        env["ANTHROPIC_API_KEY"] = ""
        env[_GATEWAY_MODEL_DISCOVERY] = "1"
        return env
