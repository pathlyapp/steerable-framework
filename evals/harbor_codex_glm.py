"""Harbor Codex agent pointed at the product gateway serving GLM.

Harbor's stock ``Codex`` targets OpenAI: it writes only ``openai_base_url``
into ``config.toml`` and truncates the model name to its last path segment.
Both are wrong for a gateway that keys models by ``vendor/model`` and serves
a model Codex ships no metadata for, and both fail quietly — a truncated id
becomes an unknown-model error and a missing window becomes early
compaction. ``pi-glm`` already cost a full catalog run to that class of
mistake, so this leg states the parameters instead of inheriting defaults.

Codex offers no output-token ceiling in ``config.toml``, so the 65536 cap
the other legs carry has no counterpart here. That asymmetry is a property
of the CLI, not a choice available to this file.
"""

from __future__ import annotations

from typing import Any

try:
    from typing import override
except ImportError:  # Python < 3.12 — evals unit tests still collect.
    def override(f):  # type: ignore[misc]
        return f

from harbor.agents.installed.codex import Codex

#: GLM-5.3 / Flash native window, from the product runtime's capability
#: table (``steerable_agent_runtime.model_info``). Codex has no metadata
#: for this model, so without the key it applies its own default and
#: compacts on a window the model does not have.
_GLM_CONTEXT_WINDOW = 1_048_576
#: ``STEERABLE_REASONING_EFFORT`` in ``harbor_steerable.run``. Codex's
#: ``ReasoningEffort`` enum carries ``max``, so the two legs can send the
#: same value rather than meeting at an approximation the way pi's
#: ``xhigh`` had to.
_GLM_REASONING_EFFORT = "max"


def restore_model_slug(command: str, model_name: str | None) -> str:
    """Put ``model_name`` back on a ``codex exec`` command Harbor truncated.

    ``Codex.run`` builds ``--model {self.model_name.split("/")[-1]}``
    mid-method, with no seam to override, and a gateway that keys models by
    ``vendor/model`` rejects the bare segment that leaves. Commands other
    than the agent invocation pass through unchanged.

    :param command: The shell command Harbor assembled.
    :param model_name: The full gateway model id.
    :returns: The command with its ``--model`` flag carrying the full id.
    :raises ValueError: If the agent invocation does not carry the truncated
        flag, which means Harbor stopped truncating and this rewrite would
        otherwise go on silently measuring whatever the parent now sends.
    """
    if "codex exec " not in command:
        return command
    truncated = f"--model {(model_name or '').split('/')[-1]} "
    if truncated not in command:
        raise ValueError(
            f"cannot find {truncated!r} in the codex command; Harbor no longer "
            f"truncates the model name the way this override expects: {command}"
        )
    return command.replace(truncated, f"--model {model_name} ", 1)


class CodexGlmHarborAgent(Codex):
    """Codex driving the product model with the product's request parameters."""

    @staticmethod
    @override
    def name() -> str:
        # Distinct from "codex" so a result.json says which leg produced it:
        # the stock name cannot distinguish the GPT baseline from this one.
        return "codex-glm"

    @override
    def _build_effective_config(
        self, openai_base_url: str | None = None
    ) -> dict[str, Any]:
        config = super()._build_effective_config(openai_base_url)
        config.setdefault("model_context_window", _GLM_CONTEXT_WINDOW)
        config.setdefault("model_reasoning_effort", _GLM_REASONING_EFFORT)
        return config

    @override
    async def exec_as_agent(self, environment, command: str, **kwargs) -> Any:
        return await super().exec_as_agent(
            environment, restore_model_slug(command, self.model_name), **kwargs
        )
