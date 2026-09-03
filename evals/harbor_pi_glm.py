"""Harbor Pi agent carrying the product model's own request parameters.

Harbor's stock ``Pi`` writes a ``models.json`` model entry of ``{"id": …}``
and lets Pi fill every other field from its defaults. Those defaults are
built for a model Pi ships metadata for, so a custom gateway serving GLM
gets ``contextWindow`` 128000, ``maxTokens`` 16384, and ``reasoning`` false.
All three are wrong for ``z-ai/glm-5.3-flash`` and all three depress the
score for reasons that have nothing to do with Pi's harness, which is the
one thing a pi-glm leg exists to measure.

Catalog run 33587641909 is the evidence: 22 of 54 trials emitted at least
16000 output tokens and one ``pi.txt`` recorded ``"reasoning": 16314``
against the 16384 cap, i.e. GLM spent the whole budget thinking and had
nothing left to answer or call a tool with. That leg scored 18/54 where
steerable averages 44/54 on the same tasks.
"""

from __future__ import annotations

from typing import Any

try:
    from typing import override
except ImportError:  # Python < 3.12 — evals unit tests still collect.
    def override(f):  # type: ignore[misc]
        return f

from harbor.agents.installed.pi import Pi
from harbor.agents.model_connection import ResolvedModelConnection

#: GLM-5.3 / Flash native window, from the product runtime's capability
#: table (``steerable_agent_runtime.model_info``). Pi's 128000 default
#: clamps ``maxTokens`` once a prompt passes ~58k tokens and starts
#: compacting eight times earlier than the model requires.
_GLM_CONTEXT_WINDOW = 1_048_576
#: ``STEERABLE_MAX_TOKENS`` in ``harbor_steerable.run``. Parity, not a
#: tunable: a different output cap makes the two legs different runs.
_GLM_MAX_TOKENS = 65_536
#: ``STEERABLE_TEMPERATURE`` in ``harbor_steerable.run``.
_GLM_TEMPERATURE = 1.0
#: Harbor's ``--thinking`` enum onto the three efforts GLM accepts
#: (``model_info`` records ``low``/``high``/``max``). ``xhigh`` is the
#: highest Harbor offers and ``max`` is what the steerable leg sends, so
#: they have to be the pair that meets.
_THINKING_LEVEL_MAP = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "max",
}


class PiGlmHarborAgent(Pi):
    """Pi driving the product model with the product's request parameters."""

    @staticmethod
    @override
    def name() -> str:
        # Distinct from "pi" so a result.json says which leg produced it:
        # the stock name cannot distinguish the Claude baseline from this one.
        return "pi-glm"

    @override
    def _build_custom_models_json(
        self,
        access: ResolvedModelConnection,
        model_id: str,
    ) -> dict[str, Any] | None:
        models_json = super()._build_custom_models_json(access, model_id)
        if models_json is None:
            # No configured base URL: the parent already refused when
            # ``model_api`` was set, and without a custom endpoint there is
            # no generated model entry to correct.
            return None
        provider = next(iter(models_json["providers"].values()))
        provider["models"] = [
            {
                **provider["models"][0],
                "contextWindow": _GLM_CONTEXT_WINDOW,
                "maxTokens": _GLM_MAX_TOKENS,
                # Without this Pi reports ["off"] as the only supported
                # thinking level, clamps --thinking to off, and sends no
                # reasoning field at all.
                "reasoning": True,
                "thinkingLevelMap": _THINKING_LEVEL_MAP,
                "samplingParams": {"temperature": _GLM_TEMPERATURE},
                "compat": {
                    # The gateway hostname is neither openrouter.ai nor
                    # z.ai, so Pi's autodetect picks the plain OpenAI
                    # dialect. Say so explicitly: this is the dialect the
                    # steerable leg speaks (``reasoning_effort: max``).
                    "thinkingFormat": "openai",
                    "supportsReasoningEffort": True,
                    # Autodetect would choose max_completion_tokens for an
                    # unrecognised host; Z.AI honours max_tokens.
                    "maxTokensField": "max_tokens",
                    # STEERABLE_OPENROUTER_PROVIDER / _ALLOW_FALLBACKS.
                    # OpenRouter's cheapest GLM route is not Z.AI, and the
                    # published TB score is the Z.AI endpoint.
                    "openRouterRouting": {
                        "order": ["z-ai"],
                        "allow_fallbacks": False,
                    },
                },
            }
        ]
        return models_json
