"""W2.1: observation aging — tiered degradation of tool results."""

from __future__ import annotations

import json

import pytest
from steerable_agent_protocol.generated import ToolCall, ToolResult

from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.observation_aging import (
    AgingRules,
    ObservationAgingHooks,
)


class _Ctx:
    def __init__(self, round_index: int) -> None:
        self.round_index = round_index
        self.chat_id = None


def _tool_message(call_id: str, data: object, *, tool: str = "read_file") -> LLMMessage:
    return LLMMessage.text_of(
        "tool",
        json.dumps({"success": True, "data": data}, ensure_ascii=False),
        name=tool,
        tool_call_id=call_id,
    )


async def _seed(hooks: ObservationAgingHooks, call_id: str, round_index: int) -> None:
    await hooks.post_tool_result(
        ToolResult(success=True, data={"x": "y" * 4000}),
        ToolCall(id=call_id, name="read_file", arguments={}),
        _Ctx(round_index),
    )


# -- rule table (2.1.1) --------------------------------------------------------


def test_rule_table_tiers() -> None:
    rules = AgingRules(fresh_rounds=3, keep_tokens=200, fold_after_rounds=8, compress_tokens=1000)
    assert rules.decide(age_rounds=1, size_tokens=5000) == "keep"  # fresh wins over size
    assert rules.decide(age_rounds=20, size_tokens=50) == "keep"  # cheap stays
    assert rules.decide(age_rounds=5, size_tokens=2000) == "compress"
    assert rules.decide(age_rounds=5, size_tokens=500) == "keep"  # mid-age small: keep
    assert rules.decide(age_rounds=8, size_tokens=2000) == "fold"
    assert rules.decide(age_rounds=30, size_tokens=2000) == "fold"


# -- hook behavior -------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_results_pass_through_without_rewrite() -> None:
    hooks = ObservationAgingHooks()
    await _seed(hooks, "c1", round_index=0)
    msg = _tool_message("c1", {"payload": "y" * 4000})
    action = await hooks.pre_step([msg], _Ctx(round_index=1))
    assert action.rewrite is None


@pytest.mark.asyncio
async def test_mid_age_large_result_compresses_keeping_schema_keys() -> None:
    hooks = ObservationAgingHooks(AgingRules(compress_tokens=100))
    await _seed(hooks, "c1", round_index=0)
    msg = _tool_message("c1", {"payload": "y" * 4000, "count": 7})
    action = await hooks.pre_step([msg], _Ctx(round_index=4))
    assert action.rewrite is not None
    assert action.rewrite.action == "observation_aging"
    aged = action.rewrite.messages[0]
    assert aged.tool_call_id == "c1"
    assert aged.role == "tool"
    payload = json.loads(aged.content_text)
    # 2.1.3: the envelope's schema keys survive.
    assert payload["success"] is True
    assert payload["data"]["count"] == 7
    assert payload["data"]["payload"].startswith("y" * 10)
    assert "[+" in payload["data"]["payload"]
    assert len(aged.content_text) < len(msg.content_text) // 4


@pytest.mark.asyncio
async def test_old_result_folds_to_expandable_reference() -> None:
    hooks = ObservationAgingHooks()
    await _seed(hooks, "c1", round_index=0)
    msg = _tool_message("c1", {"payload": "y" * 4000})
    action = await hooks.pre_step([msg], _Ctx(round_index=9))
    assert action.rewrite is not None
    aged = action.rewrite.messages[0]
    text = aged.content_text
    # 2.1.4: the stub names the tool, the round, and the record locator.
    assert "observation folded" in text
    assert "read_file" in text
    assert "round 0" in text
    assert "c1" in text


@pytest.mark.asyncio
async def test_idempotent_per_round_and_compress_to_fold_transition() -> None:
    # keep_tokens is set below the compressed size: a compressed result that
    # shrank under keep_tokens would (correctly) be cheap enough to keep
    # forever, and the compress → fold transition would never be reachable.
    hooks = ObservationAgingHooks(AgingRules(compress_tokens=100, keep_tokens=10))
    await _seed(hooks, "c1", round_index=0)
    original = _tool_message("c1", {"payload": "y" * 4000})

    first = await hooks.pre_step([original], _Ctx(round_index=4))
    assert first.rewrite is not None
    compressed = first.rewrite.messages[0]

    # Same tier next round: no new rewrite declared.
    again = await hooks.pre_step([compressed], _Ctx(round_index=5))
    assert again.rewrite is None

    # Aging further crosses into fold.
    folded = await hooks.pre_step([compressed], _Ctx(round_index=9))
    assert folded.rewrite is not None
    assert "observation folded" in folded.rewrite.messages[0].content_text

    # Fold is terminal.
    terminal = await hooks.pre_step(
        [folded.rewrite.messages[0]], _Ctx(round_index=20)
    )
    assert terminal.rewrite is None


@pytest.mark.asyncio
async def test_untracked_and_non_tool_messages_untouched() -> None:
    hooks = ObservationAgingHooks()
    seeded = _tool_message("unknown-call", {"payload": "y" * 4000})
    user = LLMMessage.text_of("user", "hello")
    action = await hooks.pre_step([user, seeded], _Ctx(round_index=50))
    assert action.rewrite is None


@pytest.mark.asyncio
async def test_small_old_results_stay_verbatim() -> None:
    hooks = ObservationAgingHooks()
    await hooks.post_tool_result(
        ToolResult(success=True, data={"v": "ok"}),
        ToolCall(id="c1", name="read_file", arguments={}),
        _Ctx(0),
    )
    msg = _tool_message("c1", "ok")
    action = await hooks.pre_step([msg], _Ctx(round_index=30))
    assert action.rewrite is None


@pytest.mark.asyncio
async def test_mixed_transcript_rewrites_only_due_messages() -> None:
    hooks = ObservationAgingHooks(AgingRules(compress_tokens=100))
    await _seed(hooks, "old", round_index=0)
    await _seed(hooks, "fresh", round_index=9)
    transcript = [
        LLMMessage.text_of("system", "sys"),
        _tool_message("old", {"payload": "y" * 4000}),
        LLMMessage.text_of("assistant", "working"),
        _tool_message("fresh", {"payload": "z" * 4000}),
    ]
    action = await hooks.pre_step(transcript, _Ctx(round_index=10))
    assert action.rewrite is not None
    out = action.rewrite.messages
    assert out[0] is transcript[0]
    assert out[2] is transcript[2]
    assert "observation folded" in out[1].content_text
    assert out[3] is transcript[3], "fresh result must pass through"
