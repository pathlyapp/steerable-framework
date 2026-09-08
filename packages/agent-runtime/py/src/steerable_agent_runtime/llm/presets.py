"""Vendor-documented optimal generation parameters, keyed by endpoint+model.

The compat matrix (``llm.compat``) pins what a vendor's wire *accepts*; this
table pins what a model runs *best* at — the sampling parameters each vendor
documents as optimal for its open-weight models. Presets are defaults, not
overrides: an explicit per-request field, a host-passed extra kwarg, or a
provider-constructed ``default_temperature`` always wins; a preset only fills
a field the caller left unset. Compat flags still gate what may be sent at
all — a preset temperature never reaches a host whose compat entry disables
the field (Moonshot's fixed-temperature models need no entry here for exactly
that reason).

Matching follows the same data-not-branches rule as the compat matrix: an
entry is a ``(host, model_prefix, preset)`` triple where either key may be
``None`` (wildcard). Sampling optima are a property of the model *weights*,
so open-weight families that travel across gateways (deepseek, qwen3, glm,
llama, gpt-oss, minimax) key on the model leaf; the base-URL host narrows an
entry when one vendor serves several model classes with divergent optima
(DeepSeek's chat vs. reasoner). Adding a vendor means adding one entry —
request building does not change (that is the acceptance bar).

Set ``STEERABLE_PROVIDER_PRESETS=0`` to disable the whole layer (the request
then carries only what the caller sent, the pre-preset behavior).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PROVIDER_PRESETS",
    "PresetEntry",
    "ProviderPreset",
    "describe_provider_presets",
    "preset_for",
    "register_provider_preset",
]


def _presets_enabled() -> bool:
    return os.environ.get("STEERABLE_PROVIDER_PRESETS", "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    """Optimal generation parameters for one provider/model family.

    Every field is a *default*: ``None`` means the preset has no opinion and
    the request goes out without the field unless the caller set it.
    ``extra_body`` carries vendor-specific fields the OpenAI schema does not
    name (e.g. ``top_k``), applied with the same fill-only-when-absent rule.
    """

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PresetEntry:
    """One registry row: match keys plus the preset to apply.

    ``host`` matches as a substring of the request base URL (the sidecar's
    provider kinds are generic, so the host is the reliable vendor signal —
    same rule as ``PROVIDER_COMPAT_HOSTS``). ``model_prefix`` matches the
    lowercased model leaf (the part after the last ``/``, so
    ``openrouter``'s ``deepseek/deepseek-chat`` still matches ``deepseek``).
    An entry with both keys set is more specific than a single-key entry;
    ties break on total key length. At least one key must be set.
    """

    host: str | None
    model_prefix: str | None
    preset: ProviderPreset

    def __post_init__(self) -> None:
        if self.host is None and self.model_prefix is None:
            raise ValueError("PresetEntry needs at least one match key")


#: Built-in preset table. Every entry is doc-sourced; the comment names the
#: document and the verification date. Entries that pin *no* parameter exist
#: to shadow a broader entry for a model class whose optimum is "send
#: nothing" (DeepSeek's reasoner).
PROVIDER_PRESETS: tuple[PresetEntry, ...] = (
    # DeepSeek reasoner class: the API fixes temperature at 1.0 in thinking
    # mode and rejects any other value with a 400, so the optimum is to send
    # nothing and let the server default apply. The empty preset shadows the
    # broader deepseek entry below. Doc-verified 2026-09-08 against
    # api-docs.deepseek.com/quick_start/parameter_settings.
    PresetEntry("api.deepseek.com", "deepseek-reasoner", ProviderPreset()),
    PresetEntry(None, "deepseek-reasoner", ProviderPreset()),
    PresetEntry(None, "deepseek-r1", ProviderPreset()),
    # DeepSeek chat class: the official temperature table recommends 0.0 for
    # Coding / Math (1.0 data analysis, 1.3 conversation, 1.5 creative) — the
    # agentic-coding value is the right default for this framework.
    # Doc-verified 2026-09-08 against the same page.
    PresetEntry("api.deepseek.com", "deepseek", ProviderPreset(temperature=0.0)),
    PresetEntry(None, "deepseek", ProviderPreset(temperature=0.0)),
    # Qwen3 thinking mode, precise-coding set: temperature 0.6, top_p 0.95,
    # top_k 20 (the non-thinking set is 0.7 / 0.8 / 20 with presence_penalty
    # 1.5; thinking is the mode agentic coding runs in, and greedy decoding
    # is documented to degrade it). top_k rides extra_body — the OpenAI
    # schema does not name it. Doc-verified 2026-09-08 against the
    # Qwen/Qwen3.6-27B and Qwen/Qwen3-32B model cards.
    PresetEntry(
        None,
        "qwen3",
        ProviderPreset(temperature=0.6, top_p=0.95, extra_body={"top_k": 20}),
    ),
    # GLM (Z.AI / bigmodel): the 4.6 and 5.2 migration guides pin
    # temperature 1.0 and top_p 0.95 as the documented defaults and recommend
    # tuning only one of them. Doc-verified 2026-09-08 against
    # docs.z.ai/guides/overview/migrate-to-glm-4.6 and migrate-to-glm-new.
    PresetEntry("api.z.ai", "glm", ProviderPreset(temperature=1.0, top_p=0.95)),
    PresetEntry("bigmodel.cn", "glm", ProviderPreset(temperature=1.0, top_p=0.95)),
    PresetEntry(None, "glm-", ProviderPreset(temperature=1.0, top_p=0.95)),
    # Meta Llama 3.x / 4 instruct: temperature 0.6, top_p 0.9 from the
    # weights' generation_config.json. Doc-verified 2026-09-08 against
    # meta-llama/llama-models models/llama3_1/MODEL_CARD.md and the Llama 4
    # Scout generation_config.
    PresetEntry(None, "llama3", ProviderPreset(temperature=0.6, top_p=0.9)),
    PresetEntry(None, "llama-3", ProviderPreset(temperature=0.6, top_p=0.9)),
    PresetEntry(None, "llama-4", ProviderPreset(temperature=0.6, top_p=0.9)),
    # OpenAI gpt-oss: temperature 1.0, top_p 1.0 per the README's recommended
    # sampling parameters; reasoning_effort medium is the documented balanced
    # default (high wants max_tokens ~30k, which a preset must not force).
    # Doc-verified 2026-09-08 against github.com/openai/gpt-oss README and
    # docs.together.ai/docs/gpt-oss.
    PresetEntry(
        None,
        "gpt-oss",
        ProviderPreset(temperature=1.0, top_p=1.0, reasoning_effort="medium"),
    ),
    # MiniMax M2 family: temperature 1.0, top_p 0.95, top_k 40 — the
    # generation_config values every M2/M2.1/M2.5 README recommends
    # explicitly passing (some gateways default to a more conservative 0.7).
    # Doc-verified 2026-09-08 against github.com/MiniMax-AI/MiniMax-M2(.1/.5).
    PresetEntry(
        None,
        "minimax",
        ProviderPreset(temperature=1.0, top_p=0.95, extra_body={"top_k": 40}),
    ),
    # Moonshot (kimi-k2.5/k2.6/k2.7-code/k3) deliberately has NO entry: every
    # sampling field is model-fixed (temperature 1.0 thinking / 0.6
    # non-thinking, top_p 0.95, penalties 0 — any other value is a 400), and
    # the host's compat entry already stops us from sending temperature at
    # all. The optimal request carries none of these fields.
)

#: Runtime-registered entries, matched before the built-in table.
_custom_entries: list[PresetEntry] = []


def register_provider_preset(entry: PresetEntry) -> None:
    """Register (or shadow) a preset at runtime.

    Custom entries are matched before the built-in table, so a deployment can
    tune a fine-tune or a newly released model without a framework release.
    """
    _custom_entries.append(entry)


def _specificity(entry: PresetEntry) -> int:
    keys = (entry.host is not None) + (entry.model_prefix is not None)
    return keys * 1000 + len(entry.host or "") + len(entry.model_prefix or "")


def preset_for(base_url: str | None, model: str | None) -> ProviderPreset | None:
    """The most specific matching preset, or ``None`` when no entry applies.

    Returns ``None`` for every input while ``STEERABLE_PROVIDER_PRESETS=0``.
    """
    if not _presets_enabled():
        return None
    url = (base_url or "").lower()
    leaf = (model or "").lower().rsplit("/", 1)[-1]
    best: PresetEntry | None = None
    best_score = -1
    # Custom entries outrank every built-in they match (the bonus), so a
    # deployment can shadow a built-in without replicating its specificity.
    for tier, table in ((1_000_000, _custom_entries), (0, PROVIDER_PRESETS)):
        for entry in table:
            if entry.host is not None and entry.host not in url:
                continue
            if entry.model_prefix is not None and not leaf.startswith(entry.model_prefix):
                continue
            score = tier + _specificity(entry)
            if score > best_score:
                best, best_score = entry, score
    return best.preset if best is not None else None


def describe_provider_presets() -> list[dict[str, Any]]:
    """Wire-level descriptor of every built-in preset for host settings UIs.

    Symmetric with ``describe_compat_flags``: hosts render from this list
    instead of hardcoding vendor knowledge, so a new table entry appears in
    host UIs without a host change.
    """
    return [
        {
            "host": entry.host,
            "modelPrefix": entry.model_prefix,
            "temperature": entry.preset.temperature,
            "topP": entry.preset.top_p,
            "maxTokens": entry.preset.max_tokens,
            "reasoningEffort": entry.preset.reasoning_effort,
            "extraBody": dict(entry.preset.extra_body) or None,
        }
        for entry in PROVIDER_PRESETS
    ]
