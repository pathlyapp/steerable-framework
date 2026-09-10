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

import os
from typing import Any

try:
    from typing import override
except ImportError:  # Python < 3.12 — evals unit tests still collect.
    def override(f):  # type: ignore[misc]
        return f

from harbor.agents.installed.pi import Pi
from harbor.agents.model_connection import ResolvedModelConnection

from evals.harbor_helpers import is_zai_glm

#: ``STEERABLE_MAX_TOKENS`` in ``harbor_steerable.run``. Parity, not a
#: tunable: a different output cap makes the two legs different runs.
_MAX_TOKENS = 65_536
#: ``STEERABLE_TEMPERATURE`` in ``harbor_steerable.run``.
_TEMPERATURE = 1.0
#: Harbor imports this module in its isolated tool env. PYTHONPATH is the
#: repo root, so ``evals.*`` resolves and ``steerable_agent_runtime`` does
#: not. Keep the window table here; do not import the runtime package.
_GLM_OR_DEEPSEEK_WINDOW = 1_048_576
_QWEN38_WINDOW = 262_144
#: Harbor's ``--thinking`` enum onto Pi's legal values ``low`` / ``high`` /
#: ``max``. GLM's catalog is ``low`` / ``high`` / ``max``, so ``xhigh``
#: maps to ``max``. DeepSeek-V4-Flash is ``high`` / ``xhigh`` — ``high``
#: stays ``high``. Qwen3.8-27B is ``low`` / ``medium`` / ``xhigh``; Pi
#: cannot emit ``medium``, so that cell is skipped on the probe matrix.
_THINKING_LEVEL_MAP = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "max",
}


def _openrouter_routing(model_id: str) -> dict[str, object]:
    """Pin the official OpenRouter provider when one is configured.

    GLM's published TB score is Z.AI, not OpenRouter's cheapest GLM route.
    A missing pin on a non-GLM model must not inherit ``z-ai`` — that
    404s every trial.
    """
    pinned = os.environ.get("STEERABLE_OPENROUTER_PROVIDER", "").strip()
    if not pinned and is_zai_glm(model_id):
        pinned = "z-ai"
    if not pinned:
        return {"allow_fallbacks": False}
    return {"order": [pinned], "allow_fallbacks": False}


def context_window_for(model_id: str) -> int:
    """Pi ``contextWindow`` for a gateway model id.

    Harbor's isolated interpreter cannot import ``steerable_agent_runtime``.
    The runtime prefix table also maps bare ``deepseek`` to 131072, which
    is the V3 window, not DeepSeek-V4-Flash's 1M.
    """
    lowered = model_id.lower()
    if "qwen3.8" in lowered or "qwen3-8-27b" in lowered:
        return _QWEN38_WINDOW
    return _GLM_OR_DEEPSEEK_WINDOW


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
                "contextWindow": context_window_for(model_id),
                "maxTokens": _MAX_TOKENS,
                # Without this Pi reports ["off"] as the only supported
                # thinking level, clamps --thinking to off, and sends no
                # reasoning field at all.
                "reasoning": True,
                "thinkingLevelMap": _THINKING_LEVEL_MAP,
                "samplingParams": {"temperature": _TEMPERATURE},
                "compat": {
                    # The gateway hostname is neither openrouter.ai nor
                    # z.ai, so Pi's autodetect picks the plain OpenAI
                    # dialect. Say so explicitly: this is the dialect the
                    # steerable leg speaks (``reasoning_effort``).
                    "thinkingFormat": "openai",
                    "supportsReasoningEffort": True,
                    # Autodetect would choose max_completion_tokens for an
                    # unrecognised host; Z.AI honours max_tokens.
                    "maxTokensField": "max_tokens",
                    "openRouterRouting": _openrouter_routing(model_id),
                },
            }
        ]
        return models_json
