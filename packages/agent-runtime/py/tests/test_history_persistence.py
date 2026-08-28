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
    CompactionBoundary,
    ContextManager,
    CoreLoop,
    HistoryItem,
    HistorySeed,
    LLMMessage,
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
    with pytest.raises(ValueError, match="unknown record entry envelope"):
        entry_from_dict({"entry": "mystery", "seq": 0})


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
