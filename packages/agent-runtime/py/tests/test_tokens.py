"""Token estimation: CJK-aware base heuristic + per-model calibration."""

from __future__ import annotations

import math

import pytest
from steerable_agent_runtime import (
    MODEL_TOKEN_FACTORS,
    estimate_text_tokens,
    estimate_tokens,
    factor_for_model,
    register_model_factor,
)
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_protocol.generated import ToolCall


@pytest.fixture(autouse=True)
def _clean_factors():
    snapshot = dict(MODEL_TOKEN_FACTORS)
    try:
        yield
    finally:
        MODEL_TOKEN_FACTORS.clear()
        MODEL_TOKEN_FACTORS.update(snapshot)


def test_empty_text() -> None:
    assert estimate_text_tokens("") == 0


def test_ascii_rule_of_four() -> None:
    # 100 ASCII chars → ceil(100 * 0.25) = 25 tokens
    assert estimate_text_tokens("a" * 100) == 25


def test_cjk_weighed_heavier() -> None:
    # 100 CJK chars → ceil(100 * 0.6) = 60 tokens (vs 25 for ASCII)
    assert estimate_text_tokens("汉" * 100) == 60
    # CJK punctuation and fullwidth forms count as CJK too
    assert estimate_text_tokens("，。" * 50) == 60
    assert estimate_text_tokens("１２３" * 33 + "１") == 60


def test_mixed_content() -> None:
    text = "查询数据库" + "a" * 40  # 5 CJK + 40 ASCII
    assert estimate_text_tokens(text) == math.ceil(5 * 0.6 + 40 * 0.25)


def test_message_overhead_and_tool_calls() -> None:
    messages = [
        LLMMessage.text_of("user", "a" * 40),  # 8 + 10
        LLMMessage.text_of(
            "assistant",
            "",
            tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 1, "b": 2})],
        ),
    ]
    # msg1: 8 + 10 = 18
    # msg2: 8 + 0 + name(3*0.25→1) + args('{"a":1,"b":2}' = 13 chars → 4)
    assert estimate_tokens(messages) == 18 + 8 + 1 + 4


def test_tool_args_serialized_compactly() -> None:
    """json.dumps default adds spaces and escapes non-ASCII — both would
    inflate the estimate vs what goes over the wire."""
    call = ToolCall(id="c1", name="查询", arguments={"关键词": "北京"})
    messages = [LLMMessage.text_of("assistant", "", tool_calls=[call])]
    # name: 2 CJK → 2 (ceil 1.2); args '{"关键词":"北京"}' = 5 CJK + 7 other
    # → ceil(5*0.6 + 7*0.25) = ceil(4.75) = 5
    assert estimate_tokens(messages) == 8 + 2 + 5


def test_model_calibration_factor() -> None:
    register_model_factor("qwen", 2.0)
    messages = [LLMMessage.text_of("user", "a" * 40)]  # base 18
    assert estimate_tokens(messages) == 18
    assert estimate_tokens(messages, model="qwen3-32b") == 36
    assert estimate_tokens(messages, model="gpt-5") == 18


def test_longest_prefix_wins() -> None:
    register_model_factor("qwen", 1.5)
    register_model_factor("qwen3", 2.0)
    assert factor_for_model("qwen3-32b") == 2.0
    assert factor_for_model("qwen2.5-7b") == 1.5
    assert factor_for_model(None) == 1.0
    assert factor_for_model("unknown") == 1.0


def test_register_validation() -> None:
    with pytest.raises(ValueError):
        register_model_factor("", 1.0)
    with pytest.raises(ValueError):
        register_model_factor("x", 0)


def test_builtin_deepseek_calibration() -> None:
    """Production-calibrated factor (2026-08-26 regression over 6.6k user-day
    buckets): base heuristic overestimates deepseek-v4 completion tokens by
    ~41% on real CJK-heavy traffic."""
    assert factor_for_model("deepseek-v4") == 0.71
    assert factor_for_model("deepseek-v3") == 0.71
    messages = [LLMMessage.text_of("user", "a" * 40)]  # base 18
    assert estimate_tokens(messages, model="deepseek-v4") == math.ceil(18 * 0.71)


def test_resolve_context_window() -> None:
    from steerable_agent_runtime.tokens import (
        DEFAULT_CONTEXT_WINDOW,
        resolve_context_window,
    )

    # Explicit config always wins.
    assert resolve_context_window("deepseek-v4", explicit=32_000) == 32_000
    # Known models resolve from the table (mirrors deeppath-api models_config).
    assert resolve_context_window("deepseek-v4") == 131_072
    assert resolve_context_window("gpt-oss:20b-cloud") == 131_072
    assert resolve_context_window("Kimi-K2.6") == 262_144
    # Unknown model / no model → conservative fallback (the old fixed default).
    assert resolve_context_window("some-finetune-9b") == DEFAULT_CONTEXT_WINDOW
    assert resolve_context_window(None) == DEFAULT_CONTEXT_WINDOW
