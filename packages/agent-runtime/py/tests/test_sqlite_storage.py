"""SqliteStorage (W2.6.1): durable zero-dependency StorageAdapter with
indexed session enumeration/search, plus the W2.6.3 guarantee that
CompactionBoundary entries round-trip byte-identically (auditability
preserved by construction — history rows are shape-agnostic JSON keyed by
seq)."""

from __future__ import annotations

import pytest
from steerable_agent_protocol.generated import (
    AgentSession,
    ChatMessage,
    HarnessTrace,
    TraceEvent,
    TraceSpan,
)
from steerable_agent_runtime.history import (
    CompactionBoundary,
    HistoryItem,
    entry_to_dict,
    kind_for_role,
)
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.resume import load_history_items
from steerable_agent_runtime.storage import SqliteStorage


@pytest.fixture
def store(tmp_path):
    storage = SqliteStorage(str(tmp_path / "test.db"))
    yield storage
    storage.close()


def _session(session_id: str, chat_id: str, **kw) -> AgentSession:
    return AgentSession(
        sessionId=session_id,
        userId=kw.get("user_id", "u1"),
        chatId=chat_id,
        currentStage="chat",
        isActive=kw.get("is_active", True),
        createdAt="2026-08-30T00:00:00Z",
        updatedAt=kw.get("updated_at", "2026-08-30T00:00:00Z"),
    )


def _message(mid: str, chat_id: str, content: str, created: str) -> ChatMessage:
    return ChatMessage(
        id=mid, chatId=chat_id, role="user", content=content, createdAt=created
    )


@pytest.mark.asyncio
async def test_session_roundtrip_and_indexed_filters(store) -> None:
    await store.upsert_session(_session("s1", "c1", updated_at="2026-08-30T01:00:00Z"))
    await store.upsert_session(
        _session("s2", "c1", is_active=False, updated_at="2026-08-30T02:00:00Z")
    )
    await store.upsert_session(
        _session("s3", "c2", user_id="u2", updated_at="2026-08-30T03:00:00Z")
    )

    loaded = await store.get_session("s2")
    assert loaded is not None and loaded.isActive is False

    # newest-first ordering, indexed filters
    assert [s.sessionId for s in await store.list_sessions()] == ["s3", "s2", "s1"]
    assert [s.sessionId for s in await store.list_sessions(chat_id="c1")] == [
        "s2",
        "s1",
    ]
    assert [s.sessionId for s in await store.list_sessions(active_only=True)] == [
        "s3",
        "s1",
    ]
    assert [s.sessionId for s in await store.list_sessions(user_id="u2")] == ["s3"]


@pytest.mark.asyncio
async def test_search_sessions_finds_by_message_content(store) -> None:
    await store.upsert_session(_session("s1", "c1"))
    await store.upsert_session(_session("s2", "c2"))
    await store.append_message(_message("m1", "c1", "deploy the staging build", "2026-08-30T01:00:00Z"))
    await store.append_message(_message("m2", "c2", "unrelated chatter", "2026-08-30T02:00:00Z"))

    hits = await store.search_sessions("staging")
    assert [s.sessionId for s in hits] == ["s1"]
    assert await store.search_sessions("absent") == []


@pytest.mark.asyncio
async def test_messages_tail_limit_matches_in_memory(store) -> None:
    for i in range(5):
        await store.append_message(
            _message(f"m{i}", "c1", f"msg {i}", f"2026-08-30T0{i}:00:00Z")
        )
    # limit keeps the NEWEST N, ascending order
    tail = await store.list_messages("c1", limit=2)
    assert [m.id for m in tail] == ["m3", "m4"]


@pytest.mark.asyncio
async def test_trace_counters_track_appends(store) -> None:
    trace = HarnessTrace(
        traceId="t1",
        chatId="c1",
        status="running",
        hadError=False,
        eventCount=0,
        spanCount=0,
        createdAt="2026-08-30T00:00:00Z",
        updatedAt="2026-08-30T00:00:00Z",
    )
    await store.upsert_trace(trace)
    await store.append_spans(
        "t1",
        [
            TraceSpan(spanId="span_1", name="llm.request", startMs=1, status="ok"),
            TraceSpan(spanId="span_2", name="add", kind="tool", startMs=2, status="ok"),
        ],
    )
    await store.append_events(
        "t1",
        [
            TraceEvent(
                eventId="e1",
                traceId="t1",
                kind="stage_start",
                name="stage_start",
                sequence=1,
                timestampMs=1,
            )
        ],
    )
    loaded = await store.get_trace("t1")
    assert loaded is not None
    assert loaded.spanCount == 2 and loaded.eventCount == 1
    assert [s.spanId for s in await store.list_spans("t1")] == ["span_1", "span_2"]


@pytest.mark.asyncio
async def test_compaction_boundary_roundtrip_byte_identical(store) -> None:
    """W2.6.3: the auditable compaction semantics survive the durable
    backend — entries come back byte-identical and the resume tail-scan
    finds the boundary and projects only post-boundary items."""
    entries = [
        entry_to_dict(
            HistoryItem(
                seq=1,
                kind=kind_for_role("user"),
                message=LLMMessage.text_of("user", "old context"),
                token_estimate=10,
            )
        ),
        entry_to_dict(
            CompactionBoundary(
                seq=2,
                reason="context budget",
                action="compact",
                replacement_count=1,
            )
        ),
        entry_to_dict(
            HistoryItem(
                seq=3,
                kind=kind_for_role("user"),
                message=LLMMessage.text_of("user", "compacted summary"),
                token_estimate=5,
            )
        ),
    ]
    await store.append_history("rec1", entries)

    # byte-identical round-trip
    loaded = await store.list_history("rec1")
    assert loaded == entries

    # exclusive after_seq: entries strictly past the boundary
    tail = await store.list_history("rec1", after_seq=2)
    assert [e["seq"] for e in tail] == [3]

    # the real resume path: boundary found by reverse scan, projection
    # contains only the post-boundary message
    items = await load_history_items(store, "rec1")
    assert items is not None
    contents = [
        "".join(p.text for p in i.message.content if p.type == "text")  # type: ignore[union-attr]
        for i in items
    ]
    assert contents == ["compacted summary"]


@pytest.mark.asyncio
async def test_history_records_enumeration(store) -> None:
    await store.append_history(
        "chat_c1_main",
        [
            entry_to_dict(
                HistoryItem(
                    seq=1,
                    kind=kind_for_role("user"),
                    message=LLMMessage.text_of("user", "hi"),
                    token_estimate=1,
                )
            )
        ],
    )
    await store.append_history(
        "chat_c1_branch_1",
        [
            entry_to_dict(
                HistoryItem(
                    seq=1,
                    kind=kind_for_role("user"),
                    message=LLMMessage.text_of("user", "branch"),
                    token_estimate=1,
                )
            )
        ],
    )
    assert await store.list_history_records() == [
        "chat_c1_branch_1",
        "chat_c1_main",
    ]
    assert await store.list_history_records(prefix="chat_c1_b") == ["chat_c1_branch_1"]


@pytest.mark.asyncio
async def test_persistence_across_reopen(tmp_path) -> None:
    path = str(tmp_path / "durable.db")
    store = SqliteStorage(path)
    await store.upsert_session(_session("s1", "c1"))
    store.close()

    reopened = SqliteStorage(path)
    loaded = await reopened.get_session("s1")
    reopened.close()
    assert loaded is not None and loaded.sessionId == "s1"
