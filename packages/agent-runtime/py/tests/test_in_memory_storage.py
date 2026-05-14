from __future__ import annotations

import pytest

from steerable_agent_protocol.generated import (
    AgentSession,
    ChatAgent,
    ChatMessage,
    HarnessTrace,
    TraceEvent,
    TraceSpan,
)
from steerable_agent_runtime.storage import InMemoryStorage


def _session(**overrides) -> AgentSession:
    base = dict(
        sessionId="s1",
        userId="u1",
        chatId="c1",
        currentStage="plan",
        isActive=True,
        createdAt="2026-01-01T00:00:00+00:00",
        updatedAt="2026-01-01T00:00:00+00:00",
    )
    base.update(overrides)
    return AgentSession(**base)


def _chat_message(**overrides) -> ChatMessage:
    base = dict(
        id="m1",
        chatId="c1",
        role="user",
        content="hi",
        createdAt="2026-01-01T00:00:00+00:00",
    )
    base.update(overrides)
    return ChatMessage(**base)


def _trace(**overrides) -> HarnessTrace:
    base = dict(
        traceId="tr1",
        status="running",
        createdAt="2026-01-01T00:00:00+00:00",
        updatedAt="2026-01-01T00:00:00+00:00",
        eventCount=0,
        spanCount=0,
        hadError=False,
    )
    base.update(overrides)
    return HarnessTrace(**base)


@pytest.mark.asyncio
async def test_session_round_trip_and_filters() -> None:
    storage = InMemoryStorage()
    s1 = _session()
    s2 = _session(
        sessionId="s2",
        userId="u2",
        chatId="c2",
        isActive=False,
        updatedAt="2026-01-02T00:00:00+00:00",
    )
    await storage.upsert_session(s1)
    await storage.upsert_session(s2)

    assert (await storage.get_session("s1")).userId == "u1"
    assert await storage.get_session("missing") is None

    by_user = await storage.list_sessions(user_id="u2")
    assert [s.sessionId for s in by_user] == ["s2"]

    by_chat = await storage.list_sessions(chat_id="c1")
    assert [s.sessionId for s in by_chat] == ["s1"]

    active = await storage.list_sessions(active_only=True)
    assert [s.sessionId for s in active] == ["s1"]

    all_sessions = await storage.list_sessions()
    assert [s.sessionId for s in all_sessions] == ["s2", "s1"]


@pytest.mark.asyncio
async def test_session_upsert_is_isolated_from_caller_mutation() -> None:
    storage = InMemoryStorage()
    session = _session()
    await storage.upsert_session(session)
    session.currentStage = "execute"
    fetched = await storage.get_session("s1")
    assert fetched is not None
    assert fetched.currentStage == "plan"


@pytest.mark.asyncio
async def test_agent_round_trip_and_filters() -> None:
    storage = InMemoryStorage()
    base = dict(
        name="Coordinator",
        createdAt="2026-01-01T00:00:00+00:00",
        updatedAt="2026-01-01T00:00:00+00:00",
    )
    a1 = ChatAgent(id="a1", sortOrder=2, **base)
    a2 = ChatAgent(id="a2", sortOrder=1, isArchived=True, **base)
    await storage.upsert_agent(a1)
    await storage.upsert_agent(a2)

    assert (await storage.get_agent("a1")).name == "Coordinator"
    listed = await storage.list_agents()
    assert [a.id for a in listed] == ["a1"]
    listed_all = await storage.list_agents(include_archived=True)
    assert {a.id for a in listed_all} == {"a1", "a2"}


@pytest.mark.asyncio
async def test_messages_appended_in_arrival_order() -> None:
    storage = InMemoryStorage()
    msg1 = _chat_message(id="m1", createdAt="2026-01-01T00:00:00+00:00")
    msg2 = _chat_message(id="m2", role="assistant", content="hello", createdAt="2026-01-01T00:01:00+00:00")
    await storage.append_message(msg1)
    await storage.append_message(msg2)

    listed = await storage.list_messages("c1")
    assert [m.id for m in listed] == ["m1", "m2"]

    limited = await storage.list_messages("c1", limit=1)
    assert [m.id for m in limited] == ["m2"]


@pytest.mark.asyncio
async def test_trace_span_event_counts_and_ordering() -> None:
    storage = InMemoryStorage()
    trace = _trace()
    await storage.upsert_trace(trace)

    spans = [
        TraceSpan(spanId="step_1", name="llm.generate", startMs=10, status="ok"),
        TraceSpan(spanId="step_2", name="tool.exec", startMs=20, status="ok"),
    ]
    await storage.append_spans("tr1", spans)

    events = [
        TraceEvent(traceId="tr1", kind="lifecycle", name="start", sequence=2, timestampMs=15),
        TraceEvent(traceId="tr1", kind="tool_call", name="exec", sequence=1, timestampMs=10),
    ]
    await storage.append_events("tr1", events)

    listed_spans = await storage.list_spans("tr1")
    assert [s.spanId for s in listed_spans] == ["step_1", "step_2"]

    listed_events = await storage.list_events("tr1")
    assert [e.sequence for e in listed_events] == [1, 2]

    refreshed = await storage.get_trace("tr1")
    assert refreshed is not None
    assert refreshed.spanCount == 2
    assert refreshed.eventCount == 2


@pytest.mark.asyncio
async def test_message_requires_chat_id() -> None:
    from steerable_agent_runtime.errors import StorageError

    storage = InMemoryStorage()
    msg = ChatMessage(
        id="m_orphan",
        role="user",
        content="hi",
        createdAt="2026-01-01T00:00:00+00:00",
    )
    with pytest.raises(StorageError):
        await storage.append_message(msg)
