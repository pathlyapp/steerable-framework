"""W5.4.1: equivalence regression — old 12-entry prefix table vs catalog.

Every row of the legacy ``MODEL_INFOS`` prefix table is probed with a
realistic model id through the new three-tier resolver. Each divergence
from the old table must carry an explanation; the test fails if a diff
appears without one. This is the regression net for the post-arm-A
``model_info.py`` rewiring: when ``resolve_model_info`` delegates to the
catalog, this table is what proves the change is understood, not drift.
"""

from __future__ import annotations

import pytest
from steerable_agent_runtime.model_info import MODEL_INFOS
from steerable_agent_runtime.model_resolve import resolve_in_catalog

# (old prefix, probe provider, probe model id, explanation of the diff).
# explanation is "" only when old and new agree.
EQUIVALENCE_TABLE: tuple[tuple[str, str, str, str], ...] = (
    (
        "deepseek-reasoner",
        "deepseek",
        "deepseek-reasoner",
        "catalog gap: models.dev's first-party deepseek provider lists the "
        "v4 family only; legacy chat/reasoner ids need an overlay entry",
    ),
    (
        "deepseek",
        "deepseek",
        "deepseek-chat",
        "same deepseek catalog gap as deepseek-reasoner",
    ),
    (
        "z-ai/glm",
        "openrouter",
        "z-ai/glm-5.3-flash",
        "old table stale: 202,752 was a glm-4 era figure; glm-5.3-flash "
        "serves 1,310,720 via openrouter",
    ),
    (
        "gpt-oss",
        "openai",
        "gpt-oss-120b",
        "open-weight family openai does not host first-party; resolves "
        "under gateways, not provider=openai",
    ),
    (
        "llama3",
        "ollama",
        "llama3.3",
        "local daemon: models.dev tracks hosted APIs; ollama stays on "
        "register_model_info runtime overrides (W5.2.2)",
    ),
    (
        "qwen3",
        "ollama",
        "qwen3-32b",
        "same local-daemon gap as llama3",
    ),
    (
        "qwen2.5",
        "openrouter",
        "qwen2.5-vl-72b-instruct",
        "old table claimed 131,072 + tool support for the whole qwen2.5 "
        "family; the 72b VL deployment actually serves 128,000 and takes "
        "no tools — both old claims wrong, as the plan predicted",
    ),
    (
        "kimi-k2",
        "moonshotai",
        "kimi-k2-0905-preview",
        "",  # 262,144 == 262,144
    ),
    (
        "minimax",
        "minimax",
        "MiniMax-M2.7",
        "old 197,000 was M2's figure; M2.7 serves 204,800 — also corrects "
        "tool_format openai→anthropic (M2.7 speaks the anthropic wire)",
    ),
    (
        "claude",
        "anthropic",
        "claude-sonnet-4-6",
        "old 200,000 was the 4.0 era window; sonnet-4-6 serves 1,000,000",
    ),
    (
        "gpt-5",
        "openai",
        "gpt-5.5",
        "old 200,000 was gpt-5.0; gpt-5.5 serves 1,050,000",
    ),
    (
    "gpt-4",
        "openai",
        "gpt-4",
        "old 128,000 described gpt-4-turbo; the bare gpt-4 deployment is "
        "8,192 — the prefix over-claimed for years",
    ),
)

_OLD_WINDOWS = {info.pattern: info.context_window for info in MODEL_INFOS}


@pytest.mark.parametrize(
    ("prefix", "provider", "model", "explanation"), EQUIVALENCE_TABLE
)
def test_prefix_row_against_catalog(
    prefix: str, provider: str, model: str, explanation: str
) -> None:
    old_window = _OLD_WINDOWS[prefix]
    hit = resolve_in_catalog(provider, model)
    new_window = hit.context_window if hit else None
    if new_window == old_window:
        assert not explanation, (
            f"{prefix}: window agrees ({old_window}) but an explanation was "
            "written — drop it so the table only documents real diffs"
        )
    else:
        assert explanation, (
            f"{prefix}: old={old_window:,} new={new_window!r} diverges and "
            "no explanation is recorded — W5.4.1 requires one per row"
        )


def test_table_covers_every_legacy_prefix() -> None:
    assert {row[0] for row in EQUIVALENCE_TABLE} == set(_OLD_WINDOWS)


def test_dated_variant_resolves_by_prefix() -> None:
    """The prefix tier's real job: ids newer than the catalog snapshot."""
    hit = resolve_in_catalog("openai", "gpt-5.5-2099-01-01")
    assert hit is not None
    assert hit.source == "prefix"
    assert hit.context_window == 1_050_000


@pytest.mark.asyncio
async def test_compaction_threshold_follows_the_catalog() -> None:
    """W5.4.2 — the chapter's real payoff and its joint with W2: the same
    transcript must compact under a 4k-window catalog model and proceed
    under a 1M-window one."""
    from steerable_agent_runtime.compaction import CompactionHooks
    from steerable_agent_runtime.llm import LLMMessage
    from steerable_agent_runtime.tokens import resolve_context_window

    class _Ctx:
        round_index = 0
        last_prompt_tokens = None
        last_prompt_transcript_len = 0

    # ~10k tokens by the chars/4 heuristic.
    transcript = [LLMMessage.text_of("user", "x" * 40_000)]

    small = CompactionHooks(
        max_context_tokens=resolve_context_window(
            "qwen-math-plus", provider="alibaba-cn"
        )
    )
    big = CompactionHooks(
        max_context_tokens=resolve_context_window(
            "claude-sonnet-4-6", provider="anthropic"
        )
    )
    assert small._max_tokens == 4_096 and big._max_tokens == 1_000_000

    small_action = await small.pre_step(list(transcript), _Ctx())
    big_action = await big.pre_step(list(transcript), _Ctx())
    # Compaction is expressed as proceed + rewrite attachment.
    assert small_action.rewrite is not None  # 10k >> 0.8 * 4k
    assert big_action.rewrite is None and big_action.kind == "proceed"
