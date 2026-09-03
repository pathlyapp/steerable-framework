"""Harbor Claude Code agent pointed at the product gateway serving GLM.

Harbor's stock ``ClaudeCode`` already does the right thing once
``ANTHROPIC_BASE_URL`` is set: it keeps the full gateway model id and pins
the sonnet, opus, haiku, and subagent aliases to it, and unlike Pi it
writes no context-window or output-cap metadata of its own. So this leg
needs no parameter repair, only a distinct name and a guard.

The guard matters because the failure it prevents is silent. With no
configured base URL the parent truncates ``z-ai/glm-5.3-flash`` to
``glm-5.3-flash`` and sends it to ``api.anthropic.com``, which answers with
an unknown-model error that reads like a task failure rather than a
misconfigured leg.
"""

from __future__ import annotations

try:
    from typing import override
except ImportError:  # Python < 3.12 — evals unit tests still collect.
    def override(f):  # type: ignore[misc]
        return f

from harbor.agents.installed.claude_code import ClaudeCode


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
