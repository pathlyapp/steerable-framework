"""Wave 1 step 5: durable record channel + O(tail) resume.

Covers the serialization codec (entry_to_dict / entry_from_dict), the
ContextManager pending-queue the loop flushes, the StorageAdapter history
method group (InMemory reference), the loop's flush wiring (continuous
per-chat log semantics), and resume.load_history_transcript's
boundary-aware tail projection.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall

from steerable_agent_runtime import (
    RECORD_FORMAT_VERSION,
    CompactionBoundary,
    ContextManager,
    CoreLoop,
    HistoryItem,
    HistorySeed,
    LLMMessage,
    RecordFormatError,
    RewriteRequest,
    RouterToolExecutor,
    ToolRouter,
    entry_from_dict,
    entry_to_dict,
    load_history_transcript,
    message_from_dict,
    message_to_dict,
    tool,
)
from steerable_agent_runtime.hooks import NoopHooks, PreStepAction
from steerable_agent_runtime.llm import LLMStreamChunk
from steerable_agent_runtime.llm.parts import ImagePart, TextPart
from steerable_agent_runtime.storage import InMemoryStorage


def _msg(role: str, text: str) -> LLMMessage:
    return LLMMessage.text_of(role, text)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Serialization codec
# ---------------------------------------------------------------------------


def test_message_codec_roundtrip_full_fidelity() -> None:
    message = LLMMessage(
        role="assistant",
        content=[
            TextPart("look at this"),
            ImagePart.from_base64("aGVsbG8=", media_type="image/png"),
        ],
        name="assistant",
        tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "hi"})],
    )
    assert message_from_dict(message_to_dict(message)) == message

    tool_msg = LLMMessage.text_of("tool", "result body", name="echo", tool_call_id="c1")
    assert message_from_dict(message_to_dict(tool_msg)) == tool_msg


def test_entry_codec_roundtrip_all_kinds() -> None:
    entries = [
        HistoryItem(
            seq=0,
            kind="user",
            message=_msg("user", "hello"),
            token_estimate=5,
            turn_id="t1",
        ),
        CompactionBoundary(seq=1, reason="compact", action="compact", turn_id="t1"),
        HistorySeed(
            seq=2,
            messages=(_msg("user", "seeded"), _msg("assistant", "prior answer")),
            token_estimate=12,
            source_record_id="chat_1",
            source_until_seq=40,
            turn_id="t2",
        ),
    ]
    for entry in entries:
        assert entry_from_dict(entry_to_dict(entry)) == entry


def test_entry_from_dict_rejects_unknown_envelope() -> None:
    with pytest.raises(RecordFormatError, match="unknown record entry envelope"):
        entry_from_dict({"entry": "mystery", "seq": 0, "v": RECORD_FORMAT_VERSION})


def test_boundary_codec_roundtrips_replacement_count() -> None:
    # W6-10: the see-through reconcile needs the rewrite size persisted.
    boundary = CompactionBoundary(
        seq=5, reason="compact", action="compact", turn_id="t1", replacement_count=4
    )
    data = entry_to_dict(boundary)
    assert data["replacement_count"] == 4
    assert entry_from_dict(data) == boundary


def test_boundary_codec_reads_legacy_without_replacement_count() -> None:
    # Records written before W6-10 lack the key — read as None (opaque
    # boundary: the see-through reconcile treats it as a real revision).
    legacy = {
        "entry": "boundary",
        "v": RECORD_FORMAT_VERSION,
        "seq": 5,
        "kind": "compaction.boundary",
        "turn_id": "t1",
        "reason": "compact",
        "action": "compact",
    }
    boundary = entry_from_dict(legacy)
    assert isinstance(boundary, CompactionBoundary)
    assert boundary.replacement_count is None
    # And a None replacement_count is omitted on write (stays optional/additive).
    assert "replacement_count" not in entry_to_dict(boundary)


def test_entry_codec_stamps_and_reads_format_version() -> None:
    # Writers stamp v=RECORD_FORMAT_VERSION; the reader round-trips it.
    entry = HistoryItem(
        seq=0, kind="user", message=_msg("user", "hi"), token_estimate=1
    )
    data = entry_to_dict(entry)
    assert data["v"] == RECORD_FORMAT_VERSION
    assert entry_from_dict(data) == entry


def test_entry_from_dict_accepts_pre_versioning_v1() -> None:
    # Records written before versioning have no ``v`` key — read as v1.
    legacy = {
        "entry": "item",
        "seq": 3,
        "kind": "user",
        "turn_id": "t1",
        "token_estimate": 2,
        "message": {"role": "user", "content": [{"type": "text", "text": "old"}]},
    }
    entry = entry_from_dict(legacy)
    assert isinstance(entry, HistoryItem)
    assert entry.seq == 3 and entry.message.content_text == "old"


def test_entry_from_dict_refuses_newer_version_fail_closed() -> None:
    # The desktop-downgrade case: a record written by a newer build must be
    # refused whole, never silently truncated or partially read.
    future = {
        "entry": "item",
        "v": RECORD_FORMAT_VERSION + 1,
        "seq": 0,
        "kind": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": "x"}]},
    }
    with pytest.raises(RecordFormatError, match="unsupported record format version"):
        entry_from_dict(future)


def test_entry_from_dict_refuses_non_integer_version() -> None:
    with pytest.raises(RecordFormatError, match="unsupported record format version"):
        entry_from_dict({"entry": "item", "v": "2", "seq": 0})


# ---------------------------------------------------------------------------
# ContextManager: pending queue + seed projection
# ---------------------------------------------------------------------------


def test_drain_pending_returns_new_entries_once() -> None:
    manager = ContextManager([_msg("user", "a")])
    first = manager.drain_pending()
    assert len(first) == 1 and isinstance(first[0], HistoryItem)
    manager.append(_msg("assistant", "b"))
    manager.replace_all([_msg("user", "summary")], reason="compact")
    second = manager.drain_pending()
    # assistant item + boundary + replacement item
    assert [type(e) for e in second] == [HistoryItem, CompactionBoundary, HistoryItem]
    assert manager.drain_pending() == []


def test_mark_persisted_prefix_drops_already_durable_seed() -> None:
    manager = ContextManager([_msg("user", "a"), _msg("assistant", "b")])
    manager.mark_persisted_prefix(2)
    manager.append(_msg("user", "c"))
    pending = manager.drain_pending()
    assert len(pending) == 1
    assert isinstance(pending[0], HistoryItem)
    assert pending[0].message.content_text == "c"
    with pytest.raises(ValueError, match="persisted prefix"):
        manager.mark_persisted_prefix(99)


def test_seed_expands_inline_in_projection() -> None:
    manager = ContextManager()
    seed = manager.seed(
        [_msg("user", "goal"), _msg("assistant", "prior")],
        source_record_id="chat_1",
        source_until_seq=7,
    )
    assert seed.kind == "history.seed"
    manager.append(_msg("user", "continue"))
    assert [m.content_text for m in manager.projection] == [
        "goal",
        "prior",
        "continue",
    ]
    assert manager.projection_token_estimate > 0
    # A later rewrite supersedes the seed like any other entry.
    manager.replace_all([_msg("user", "compacted")], reason="compact")
    assert [m.content_text for m in manager.projection] == ["compacted"]


# ---------------------------------------------------------------------------
# StorageAdapter history method group (InMemory reference)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_memory_history_range_and_reverse_paging() -> None:
    storage = InMemoryStorage()
    manager = ContextManager([_msg("user", f"m{i}") for i in range(5)])
    await storage.append_history(
        "chat_1", [entry_to_dict(e) for e in manager.drain_pending()]
    )

    forward = await storage.list_history("chat_1")
    assert [e["seq"] for e in forward] == [0, 1, 2, 3, 4]

    tail = await storage.list_history("chat_1", after_seq=2)
    assert [e["seq"] for e in tail] == [3, 4]

    bounded = await storage.list_history("chat_1", until_seq=1)
    assert [e["seq"] for e in bounded] == [0, 1]

    newest_first = await storage.list_history("chat_1", reverse=True, limit=2)
    assert [e["seq"] for e in newest_first] == [4, 3]

    # Reverse paging cursor: the next page continues below the oldest seen.
    next_page = await storage.list_history(
        "chat_1", until_seq=newest_first[-1]["seq"] - 1, reverse=True, limit=2
    )
    assert [e["seq"] for e in next_page] == [2, 1]

    assert await storage.list_history("unknown") == []


# ---------------------------------------------------------------------------
# Loop flush wiring — the continuous per-chat log
# ---------------------------------------------------------------------------


def _provider(script: list[dict[str, Any]]):
    class _FakeProvider:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self.calls: list[list[LLMMessage]] = []
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            self.calls.append(list(messages))
            entry = script[min(self._idx, len(script) - 1)]
            self._idx += 1

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                if entry.get("content"):
                    yield LLMStreamChunk(content_delta=entry["content"])
                for call in entry.get("tool_calls", []):
                    yield LLMStreamChunk(tool_call_delta=call)
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop"
                )

            return _gen()

    return _FakeProvider()


async def _collect(loop_run: AsyncIterator) -> list[Any]:
    return [e async for e in loop_run]


@pytest.mark.asyncio
async def test_loop_persists_full_fidelity_record() -> None:
    router = ToolRouter()

    @tool(router=router, description="Echo text")
    async def echo(text: str) -> dict[str, str]:
        return {"echo": text}

    storage = InMemoryStorage()
    provider = _provider(
        [
            {"tool_calls": [ToolCall(id="c1", name="echo", arguments={"text": "hi"})]},
            {"content": "final answer"},
        ]
    )
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(loop.run([_msg("user", "echo hi")], chat_id="chat_1"))

    entries = await storage.list_history("chat_1")
    kinds = [e["kind"] for e in entries]
    assert kinds == ["user", "assistant", "tool", "assistant"]
    # Full fidelity: the tool result carries the real ToolResult envelope
    # (success + value), not a 300-char display preview.
    tool_entry = entries[2]
    tool_text = tool_entry["message"]["content"][0]["text"]
    assert tool_entry["message"]["content"][0]["type"] == "text"
    assert '"success": true' in tool_text and '"echo": "hi"' in tool_text
    assert tool_entry["message"]["tool_call_id"] == "c1"
    # The terminal assistant message is recorded (resume completeness).
    assert entries[3]["message"]["content"] == [
        {"type": "text", "text": "final answer"}
    ]
    # Resume from the record reproduces what the model last saw PLUS the
    # terminal answer it produced (the record is complete for resume).
    resumed = await load_history_transcript(storage, "chat_1")
    assert resumed == [*provider.calls[-1], _msg("assistant", "final answer")]


@pytest.mark.asyncio
async def test_continuous_log_persists_only_the_turn_delta() -> None:
    """Turn 2 seeds with turn 1's projection + the new user message; only
    the new items may flush — the record is one continuous per-chat log."""
    storage = InMemoryStorage()
    provider1 = _provider([{"content": "answer one"}])
    loop1 = CoreLoop(
        provider1,
        RouterToolExecutor(ToolRouter()),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(loop1.run([_msg("user", "question one")], chat_id="chat_1"))
    assert len(await storage.list_history("chat_1")) == 2

    turn2_seed = [*loop1.history.projection, _msg("user", "question two")]
    provider2 = _provider([{"content": "answer two"}])
    loop2 = CoreLoop(
        provider2,
        RouterToolExecutor(ToolRouter()),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(loop2.run(turn2_seed, chat_id="chat_1"))

    entries = await storage.list_history("chat_1")
    # 2 (turn 1) + new user message + turn 2's terminal assistant.
    assert len(entries) == 4
    # seq is monotonic, not dense: turn 2's in-memory seed items took seqs
    # 2-3 but were already durable (as 0-1), so only the delta flushed.
    assert [e["seq"] for e in entries] == [0, 1, 4, 5]
    assert entries[2]["message"]["content"] == [
        {"type": "text", "text": "question two"}
    ]
    # And the durable projection is the full conversation.
    resumed = await load_history_transcript(storage, "chat_1")
    assert [m.content_text for m in resumed] == [
        "question one",
        "answer one",
        "question two",
        "answer two",
    ]


@pytest.mark.asyncio
async def test_host_revision_declares_a_boundary_before_reseeding() -> None:
    """The host rewrote history between turns (edit/regenerate upstream):
    the loop records a host_revision boundary so the durable projection
    stays coherent instead of doubling the seed."""
    storage = InMemoryStorage()
    loop1 = CoreLoop(
        _provider([{"content": "answer one"}]),
        RouterToolExecutor(ToolRouter()),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(loop1.run([_msg("user", "question one")], chat_id="chat_1"))

    # Host edited the first user message → seed is not the recorded prefix.
    loop2 = CoreLoop(
        _provider([{"content": "answer two"}]),
        RouterToolExecutor(ToolRouter()),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(
        loop2.run(
            [_msg("user", "edited question"), _msg("assistant", "edited answer"),
             _msg("user", "follow-up")],
            chat_id="chat_1",
        )
    )

    entries = await storage.list_history("chat_1")
    boundary = [e for e in entries if e["entry"] == "boundary"]
    assert len(boundary) == 1
    assert boundary[0]["action"] == "host_revision"
    assert boundary[0]["seq"] == 2  # right after turn 1's two entries
    # The durable projection is exactly the revised conversation.
    resumed = await load_history_transcript(storage, "chat_1")
    assert [m.content_text for m in resumed] == [
        "edited question",
        "edited answer",
        "follow-up",
        "answer two",
    ]


@pytest.mark.asyncio
async def test_host_shaped_reseed_reconciles_without_a_boundary() -> None:
    """Production hosts rebuild a lossy view per turn (final user/assistant
    texts only — no tool rounds, no injected fragments). The seed
    reconciles against the record's host-visible view: no spurious
    host_revision, the record stays delta-only, and the model keeps the
    full history (tool rounds included)."""
    storage = InMemoryStorage()
    router = ToolRouter()

    @tool(router=router, description="Echo text")
    async def echo(text: str) -> dict[str, str]:
        return {"echo": text}

    provider1 = _provider(
        [
            {"tool_calls": [ToolCall(id="c1", name="echo", arguments={"text": "x"})]},
            {"content": "final answer one"},
        ]
    )
    loop1 = CoreLoop(
        provider1,
        RouterToolExecutor(router),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(loop1.run([_msg("user", "question one")], chat_id="chat_1"))
    # user, assistant(tool_calls), tool result, terminal assistant.
    assert len(await storage.list_history("chat_1")) == 4

    # Turn 2's seed is the host-DB view: the tool round is absent, plus the
    # new user input.
    provider2 = _provider([{"content": "answer two"}])
    loop2 = CoreLoop(
        provider2,
        RouterToolExecutor(router),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(
        loop2.run(
            [
                _msg("user", "question one"),
                _msg("assistant", "final answer one"),
                _msg("user", "question two"),
            ],
            chat_id="chat_1",
        )
    )

    entries = await storage.list_history("chat_1")
    assert not [e for e in entries if e["entry"] == "boundary"]
    # Only the genuinely new items flushed: the new user message and turn
    # 2's terminal answer.
    assert len(entries) == 6
    # The model saw the full record history — turn 1's tool round included.
    request2 = provider2.calls[0]
    assert [m.role for m in request2] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert request2[1].tool_calls is not None
    assert request2[-1].content_text == "question two"


@pytest.mark.asyncio
async def test_host_assistant_display_suffix_reconciles() -> None:
    """Hosts append display sections (executed actions, tool summaries) to
    recent assistant texts; reconciliation tolerates the appended suffix."""
    storage = InMemoryStorage()
    loop1 = CoreLoop(
        _provider([{"content": "answer one"}]),
        RouterToolExecutor(ToolRouter()),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(loop1.run([_msg("user", "question one")], chat_id="chat_1"))

    loop2 = CoreLoop(
        _provider([{"content": "answer two"}]),
        RouterToolExecutor(ToolRouter()),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(
        loop2.run(
            [
                _msg("user", "question one"),
                _msg("assistant", "answer one\n\n---\n**已执行**: echo"),
                _msg("user", "question two"),
            ],
            chat_id="chat_1",
        )
    )

    entries = await storage.list_history("chat_1")
    assert not [e for e in entries if e["entry"] == "boundary"]
    assert len(entries) == 4  # turn 1's pair + turn 2's pair


@pytest.mark.asyncio
async def test_truncated_host_seed_still_declares_a_boundary() -> None:
    """Regenerate/edit truncate the host history — a shorter seed is a
    genuine revision, not a continuation."""
    storage = InMemoryStorage()
    loop1 = CoreLoop(
        _provider([{"content": "answer one"}]),
        RouterToolExecutor(ToolRouter()),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(loop1.run([_msg("user", "question one")], chat_id="chat_1"))

    loop2 = CoreLoop(
        _provider([{"content": "answer two"}]),
        RouterToolExecutor(ToolRouter()),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(loop2.run([_msg("user", "different question")], chat_id="chat_1"))

    entries = await storage.list_history("chat_1")
    boundary = [e for e in entries if e["entry"] == "boundary"]
    assert len(boundary) == 1
    assert boundary[0]["action"] == "host_revision"
    resumed = await load_history_transcript(storage, "chat_1")
    assert [m.content_text for m in resumed] == ["different question", "answer two"]


@pytest.mark.asyncio
async def test_forked_record_reconciles_host_shaped_seeds() -> None:
    """A fork's seed carries per-message kinds, so the next host-shaped
    turn on the forked record reconciles instead of declaring a spurious
    revision."""
    from steerable_agent_runtime.resume import load_history_items

    storage = InMemoryStorage()
    router = ToolRouter()

    @tool(router=router, description="Echo text")
    async def echo(text: str) -> dict[str, str]:
        return {"echo": text}

    provider1 = _provider(
        [
            {"tool_calls": [ToolCall(id="c1", name="echo", arguments={"text": "x"})]},
            {"content": "final answer one"},
        ]
    )
    loop1 = CoreLoop(
        provider1,
        RouterToolExecutor(router),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(loop1.run([_msg("user", "question one")], chat_id="chat_1"))

    # Fork: seed a fresh record from the source projection, kinds preserved.
    items = await load_history_items(storage, "chat_1")
    assert items is not None
    seed = HistorySeed(
        seq=0,
        messages=tuple(item.message for item in items),
        token_estimate=0,
        source_record_id="chat_1",
        message_kinds=tuple(item.kind for item in items),
    )
    await storage.append_history("chat_1:fork", [entry_to_dict(seed)])

    # The host-shaped turn (no tool round in the seed) reconciles.
    loop2 = CoreLoop(
        _provider([{"content": "answer two"}]),
        RouterToolExecutor(router),
        history_store=storage,
        record_id="chat_1:fork",
    )
    await _collect(
        loop2.run(
            [
                _msg("user", "question one"),
                _msg("assistant", "final answer one"),
                _msg("user", "try again"),
            ],
            chat_id="chat_1:fork",
        )
    )

    entries = await storage.list_history("chat_1:fork")
    assert not [e for e in entries if e["entry"] == "boundary"]
    resumed = await load_history_transcript(storage, "chat_1:fork")
    assert [m.content_text for m in resumed][-2:] == ["try again", "answer two"]


@pytest.mark.asyncio
async def test_w6_10_framework_compaction_survives_host_reseed() -> None:
    """W6-10 修复实证:框架回合内压缩在桌面跨回合重种子下被保留。

    生产接线里(sidecar CoreLoop 是唯一聊天路径)两层压缩操作同一对话:

    - 框架 `CompactionHooks` 在回合内压缩自己的 durable record(写
      `CompactionBoundary`,把中段 user/assistant 摘要成一个 marker);
    - 桌面只把每轮的最终 user/assistant 文本落到自己的 `chat_messages`,
      看不到框架的压缩边界,下一轮仍按原始(未压缩)历史重种子。

    修复前:桌面的原始重种子与框架已压缩的投影对不上 → reconcile 失败 →
    `host_revision` 边界 → 框架丢弃自己的压缩 → 同一段中段被再次压缩
    (双层重复压缩)。

    修复后:reconcile 增加「看穿框架自身压缩」的回退——`compact` 边界
    携带 `replacement_count`,据此还原出桌面视角的原始对话,与桌面的
    原始重种子比对成功 → 不写 `host_revision`,框架压缩被保留,只追加
    真正的新消息。本测试实证修复后的行为。
    """
    from steerable_agent_runtime import CompactionHooks

    storage = InMemoryStorage()

    # 长会话第 1 轮:桌面发来一段已有历史的种子(纯文本,无 tool 消息——
    # 折叠无从下手,直接走中段摘要)。窗口调到很小,首轮 pre_step 即触发压缩。
    def turn1_seed() -> list[LLMMessage]:
        return [
            _msg("user", "目标:重构存储层 " + "细节" * 40),
            _msg("assistant", "好的,我先看一下现状 " + "分析" * 40),
            _msg("user", "先处理迁移 " + "要求" * 40),
            _msg("assistant", "迁移方案如下 " + "方案" * 40),
            _msg("user", "继续 " + "补充" * 40),
        ]

    hooks1 = CompactionHooks(
        max_context_tokens=120,  # 阈值 96 token,种子估算远超 → 立即压缩
        keep_last_messages=2,
        keep_last_tool_results=2,
        summarizer=None,  # 确定性 fallback:role + 摘录
        model="fake-model",
    )
    loop1 = CoreLoop(
        _provider([{"content": "第一轮答复 " + "内容" * 40}]),
        RouterToolExecutor(ToolRouter()),
        hooks=hooks1,
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(loop1.run(turn1_seed(), chat_id="chat_1"))

    entries1 = await storage.list_history("chat_1")
    compact1 = [e for e in entries1 if e["entry"] == "boundary" and e["action"] == "compact"]
    assert len(compact1) == 1, f"turn 1 应发生一次压缩,实际边界: {entries1}"
    # 压缩边界必须记录 replacement_count(看穿压缩回退的依据)。
    assert compact1[0].get("replacement_count") is not None, (
        f"compact 边界应记录 replacement_count,实际: {compact1[0]}"
    )
    # 压缩后的投影里,中段被替换成摘要 marker(head 首条 user 与尾部保留)。
    projection1 = await load_history_transcript(storage, "chat_1")
    assert any("[context compacted" in m.content_text for m in projection1), (
        f"turn 1 投影应含摘要 marker,实际: {[m.content_text[:30] for m in projection1]}"
    )

    # 桌面侧:只落库最终 user/assistant 文本(看不到框架的压缩 marker)。
    # 下一轮重种子 = 原始 5 条 + 新 user(对应 buildConversationMessages 发
    # 全量原始历史——桌面滚动摘要已删除,跨轮压缩完全交给框架)。
    desktop_seed_turn2 = [
        *turn1_seed(),
        _msg("assistant", "第一轮答复 " + "内容" * 40),  # turn 1 落库的最终答复
        _msg("user", "第二轮提问 " + "新内容" * 40),
    ]

    # 第 2 轮用大窗口,自身不再触发压缩——聚焦验证 reconcile 是否保留第 1 轮的压缩。
    hooks2 = CompactionHooks(
        max_context_tokens=100_000,
        keep_last_messages=2,
        keep_last_tool_results=2,
        summarizer=None,
        model="fake-model",
    )
    loop2 = CoreLoop(
        _provider([{"content": "第二轮答复"}]),
        RouterToolExecutor(ToolRouter()),
        hooks=hooks2,
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(loop2.run(desktop_seed_turn2, chat_id="chat_1"))

    entries2 = await storage.list_history("chat_1")
    host_revisions = [e for e in entries2 if e["entry"] == "boundary" and e["action"] == "host_revision"]
    compacts = [e for e in entries2 if e["entry"] == "boundary" and e["action"] == "compact"]

    # 修复结论 1:看穿压缩的 reconcile 匹配成功 → 不再写 host_revision。
    assert len(host_revisions) == 0, (
        f"修复后桌面重种子不应触发 host_revision(框架压缩被保留),实际: {entries2}"
    )
    # 修复结论 2:只有第 1 轮那一次压缩,同一段中段不会被重复压缩。
    assert len(compacts) == 1, (
        f"修复后应只有 turn 1 的一次压缩(无重复压缩),实际 compact 边界数: {len(compacts)}"
    )
    # 修复结论 3:第 2 轮投影仍保留第 1 轮的摘要 marker(压缩成果存续)。
    projection2 = await load_history_transcript(storage, "chat_1")
    assert any("[context compacted" in m.content_text for m in projection2), (
        f"turn 2 投影应保留 turn 1 的摘要 marker,实际: "
        f"{[m.content_text[:30] for m in projection2]}"
    )


@pytest.mark.asyncio
async def test_w6_10_see_through_reconcile_still_detects_genuine_host_edit() -> None:
    """W6-10 边界:看穿压缩的回退不能吞掉真正的 host 编辑。

    框架第 1 轮压缩后,若桌面下一轮种子在某条**历史**消息上真的改了内容
    (不是单纯追加),看穿压缩的视图也对不上 → 仍应声明 host_revision,
    从桌面编辑后的视图重种子。这保证回退只在「桌面只是重发了原始对话」
    时生效,不会把真实编辑误判成「框架自己压缩过」。
    """
    from steerable_agent_runtime import CompactionHooks

    storage = InMemoryStorage()

    def turn1_seed() -> list[LLMMessage]:
        return [
            _msg("user", "目标:重构存储层 " + "细节" * 40),
            _msg("assistant", "好的,我先看一下现状 " + "分析" * 40),
            _msg("user", "先处理迁移 " + "要求" * 40),
            _msg("assistant", "迁移方案如下 " + "方案" * 40),
            _msg("user", "继续 " + "补充" * 40),
        ]

    hooks1 = CompactionHooks(
        max_context_tokens=120,
        keep_last_messages=2,
        keep_last_tool_results=2,
        summarizer=None,
        model="fake-model",
    )
    loop1 = CoreLoop(
        _provider([{"content": "第一轮答复 " + "内容" * 40}]),
        RouterToolExecutor(ToolRouter()),
        hooks=hooks1,
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(loop1.run(turn1_seed(), chat_id="chat_1"))
    assert any(
        e["entry"] == "boundary" and e["action"] == "compact"
        for e in await storage.list_history("chat_1")
    ), "turn 1 应发生一次压缩"

    # 第 2 轮:桌面**编辑**了第 2 条历史消息(把 assistant 答复改掉),再追加新消息。
    edited_seed = [
        _msg("user", "目标:重构存储层 " + "细节" * 40),
        _msg("assistant", "【已编辑】推翻重来 " + "新方向" * 40),  #  genuinely edited
        _msg("user", "先处理迁移 " + "要求" * 40),
        _msg("assistant", "迁移方案如下 " + "方案" * 40),
        _msg("user", "继续 " + "补充" * 40),
        _msg("assistant", "第一轮答复 " + "内容" * 40),
        _msg("user", "第二轮提问 " + "新内容" * 40),
    ]
    loop2 = CoreLoop(
        _provider([{"content": "第二轮答复"}]),
        RouterToolExecutor(ToolRouter()),
        hooks=CompactionHooks(
            max_context_tokens=100_000,
            keep_last_messages=2,
            keep_last_tool_results=2,
            summarizer=None,
            model="fake-model",
        ),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(loop2.run(edited_seed, chat_id="chat_1"))

    entries2 = await storage.list_history("chat_1")
    host_revisions = [
        e for e in entries2 if e["entry"] == "boundary" and e["action"] == "host_revision"
    ]
    assert len(host_revisions) == 1, (
        f"真实的 host 编辑仍应触发 host_revision(看穿回退不能吞掉编辑),实际: {entries2}"
    )


@pytest.mark.asyncio
async def test_resume_after_compaction_reads_only_the_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O(tail): with a compaction boundary near the end of the record, the
    reverse scan pages back only until the boundary — the superseded span
    is never read. Page size is shrunk so an 8-entry record exercises the
    paging path (production pages are 256)."""

    from steerable_agent_runtime import resume as resume_mod

    monkeypatch.setattr(resume_mod, "_RESUME_PAGE", 2)

    class _CountingStorage(InMemoryStorage):
        def __init__(self) -> None:
            super().__init__()
            self.seqs_read: list[int] = []

        async def list_history(self, record_id, **kw):  # type: ignore[override]
            page = await super().list_history(record_id, **kw)
            self.seqs_read.extend(int(e["seq"]) for e in page)
            return page

    class _CompactOnce(NoopHooks):
        def __init__(self) -> None:
            self.done = False

        async def pre_step(self, transcript, ctx):
            if self.done or ctx.round_index == 0:
                return PreStepAction(kind="proceed")
            self.done = True
            return PreStepAction(
                kind="proceed",
                rewrite=RewriteRequest(
                    messages=[_msg("user", "compacted summary")],
                    reason="test compaction",
                ),
            )

    router = ToolRouter()

    @tool(router=router, description="Echo text")
    async def echo(text: str) -> dict[str, str]:
        return {"echo": text}

    storage = _CountingStorage()
    provider = _provider(
        [
            {"tool_calls": [ToolCall(id="c1", name="echo", arguments={"text": "x"})]},
            {"tool_calls": [ToolCall(id="c2", name="echo", arguments={"text": "y"})]},
            {"content": "done"},
        ]
    )
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        hooks=_CompactOnce(),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(loop.run([_msg("user", "start")], chat_id="chat_1"))

    entries = await storage.list_history("chat_1")
    boundary_seq = next(e["seq"] for e in entries if e["entry"] == "boundary")
    assert boundary_seq >= 3  # a real superseded span exists before it

    storage.seqs_read.clear()
    resumed = await load_history_transcript(storage, "chat_1")
    assert resumed is not None
    # The model's last view plus the terminal answer it produced.
    assert resumed == [*provider.calls[-1], _msg("assistant", "done")]
    # O(tail): the reverse scan stopped at the page containing the boundary
    # — reads stay within the visible span plus less than one page of
    # overlap, and the fully-superseded pages below are never touched.
    assert storage.seqs_read
    assert boundary_seq - min(storage.seqs_read) < 2  # the test page size


@pytest.mark.asyncio
async def test_resume_until_seq_truncates_for_fork() -> None:
    storage = InMemoryStorage()
    provider = _provider(
        [
            {"content": "first"},
        ]
    )
    loop = CoreLoop(
        provider,
        RouterToolExecutor(ToolRouter()),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(loop.run([_msg("user", "one")], chat_id="chat_1"))
    loop2 = CoreLoop(
        _provider([{"content": "second"}]),
        RouterToolExecutor(ToolRouter()),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(
        loop2.run([*loop.history.projection, _msg("user", "two")], chat_id="chat_1")
    )

    full = await load_history_transcript(storage, "chat_1")
    assert [m.content_text for m in full] == ["one", "first", "two", "second"]
    # Fork before the second turn: seq 1 is turn 1's terminal assistant.
    prefix = await load_history_transcript(storage, "chat_1", until_seq=1)
    assert [m.content_text for m in prefix] == ["one", "first"]


@pytest.mark.asyncio
async def test_resume_empty_record_returns_none() -> None:
    storage = InMemoryStorage()
    assert await load_history_transcript(storage, "nope") is None


@pytest.mark.asyncio
async def test_no_store_keeps_loop_in_memory() -> None:
    """Without a history store the record is purely in-memory (standalone)."""
    loop = CoreLoop(
        _provider([{"content": "hi"}]),
        RouterToolExecutor(ToolRouter()),
    )
    await _collect(loop.run([_msg("user", "hello")]))
    assert [m.content_text for m in loop.history.projection] == ["hello", "hi"]


# ---------------------------------------------------------------------------
# Step 6 tripwire: recorded requests vs the record (auto boundary alignment)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recorded_requests_match_record_across_declared_compaction() -> None:
    """The W1 tripwire: with a declared rewrite mid-run, the recorded
    requests still align with the record — zero manual boundary indices."""

    from steerable_agent_runtime import (
        InMemoryRequestSink,
        RecordingProvider,
        assert_requests_match_record,
    )

    class _CompactOnce(NoopHooks):
        def __init__(self) -> None:
            self.done = False

        async def pre_step(self, transcript, ctx):
            if self.done or ctx.round_index == 0:
                return PreStepAction(kind="proceed")
            self.done = True
            return PreStepAction(
                kind="proceed",
                rewrite=RewriteRequest(
                    messages=[_msg("user", "compacted summary")],
                    reason="test compaction",
                ),
            )

    router = ToolRouter()

    @tool(router=router, description="Echo text")
    async def echo(text: str) -> dict[str, str]:
        return {"echo": text}

    sink = InMemoryRequestSink()
    provider = RecordingProvider(
        _provider(
            [
                {"tool_calls": [ToolCall(id="c1", name="echo", arguments={"text": "x"})]},
                {"tool_calls": [ToolCall(id="c2", name="echo", arguments={"text": "y"})]},
                {"content": "done"},
            ]
        ),
        sink,
    )
    storage = InMemoryStorage()
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        hooks=_CompactOnce(),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(loop.run([_msg("user", "start")], chat_id="chat_1"))

    assert len(sink.requests) == 3
    entries = await storage.list_history("chat_1")
    # Passes with NO manual boundary declarations — the record's own
    # CompactionBoundary aligns request 2 (the post-compaction request).
    assert_requests_match_record(sink.requests, entries)
    # Dicts straight from storage and RecordEntry objects both work.
    assert_requests_match_record(sink.requests, [entry_from_dict(e) for e in entries])


@pytest.mark.asyncio
async def test_assert_requests_match_record_catches_undeclared_rewrite() -> None:
    """A request that matches no record projection fails loudly — the
    tripwire for mutations that bypassed the declared paths."""

    from steerable_agent_runtime import (
        InMemoryRequestSink,
        RecordingProvider,
        assert_requests_match_record,
    )

    sink = InMemoryRequestSink()
    provider = RecordingProvider(_provider([{"content": "hi"}]), sink)
    storage = InMemoryStorage()
    loop = CoreLoop(
        provider,
        RouterToolExecutor(ToolRouter()),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(loop.run([_msg("user", "hello")], chat_id="chat_1"))
    entries = await storage.list_history("chat_1")
    assert_requests_match_record(sink.requests, entries)

    # Simulate an undeclared rewrite: the recorded request's user message
    # was tampered with after the fact.
    tampered = [dict(m) for m in sink.requests[0].messages]
    tampered[0] = {**tampered[0], "content": "edited behind the record's back"}
    sink.requests[0].messages = tampered
    with pytest.raises(AssertionError, match="matches no record projection"):
        assert_requests_match_record(sink.requests, entries)
