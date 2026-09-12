"""Harbor Codex agent pointed at the product gateway.

Harbor 0.22.0's stock ``Codex`` always passes ``--model`` as
``model_name.split("/")[-1]`` (harbor#3013). That is correct against
api.openai.com (``openai/gpt-5.5`` → ``gpt-5.5``). It is wrong against an
OpenAI-compatible gateway whose ids themselves contain a slash:
``openai/z-ai/glm-5.3-flash`` becomes ``glm-5.3-flash``, which OpenRouter
does not serve.

Stock ``codex`` stays the Monday LIVE_AGENTS baseline (official key,
Responses API). This leg is catalog/probe only: GHA maps STEERABLE_* onto
OPENAI_* only when the dispatched agent is this one, so the gateway key
never lands on the official Codex smoke.
"""

from __future__ import annotations

try:
    from typing import override
except ImportError:  # Python < 3.12 — evals unit tests still collect.

    def override(f):  # type: ignore[misc]
        return f

from harbor.agents.installed.codex import Codex
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


def gateway_codex_model(model_name: str) -> str:
    """Strip Harbor's ``openai/`` prefix only. Nested gateway ids stay intact."""
    return model_name.removeprefix("openai/")


def rewrite_codex_exec_model(command: str, model_name: str) -> str:
    """Restore the nested id Harbor's parent truncated to the last segment."""
    if "codex exec" not in command or "--model " not in command:
        return command
    tail = model_name.split("/")[-1]
    full = gateway_codex_model(model_name)
    if not tail or full == tail:
        return command
    needle = f"--model {tail} "
    if needle not in command:
        return command
    return command.replace(needle, f"--model {full} ", 1)


class CodexGlmHarborAgent(Codex):
    """Codex CLI driving the product model through the product gateway."""

    @staticmethod
    @override
    def name() -> str:
        return "codex-glm"

    def _require_gateway(self) -> str:
        if not self.model_name:
            raise ValueError("Model name is required")
        if not self.model_connection.configured_base_url:
            raise ValueError(
                "codex-glm needs OPENAI_BASE_URL set to the product gateway. "
                "Without it Codex keeps only the last segment of "
                f"{self.model_name!r} and sends it to api.openai.com."
            )
        return gateway_codex_model(self.model_name)

    @override
    async def exec_as_agent(self, environment: BaseEnvironment, command, **kwargs):
        if isinstance(command, str) and self.model_name:
            command = rewrite_codex_exec_model(command, self.model_name)
        return await super().exec_as_agent(environment, command, **kwargs)

    @override
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        self._require_gateway()
        await super().run(instruction, environment, context)
