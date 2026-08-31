"""W3.1.4: one contract suite, every StorageAdapter implementation.

The protocol is only real if all implementations agree on the semantics
callers rely on. Pinned here:

- ``list_messages(limit=N)`` keeps the NEWEST N (tail), re-ordered ascending.
- ``list_history(after_seq=…)`` is EXCLUSIVE; ``until_seq`` inclusive;
  ``reverse=True, limit=N`` is the O(tail) resume scan (newest-first).
- ``search_sessions`` matches message content substring, newest
  ``updatedAt`` first, optional user filter.
- ``list_history_records`` enumerates record ids, optional prefix filter.

SqlAlchemyStorage runs when the optional extra is installed; the suite
skips it otherwise rather than weakening the contract.
"""

from __future__ import annotations

import pytest
from steerable_agent_protocol.generated import AgentSession, ChatMessage
from steerable_agent_runtime.storage import InMemoryStorage, SqliteStorage


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


@pytest.fixture
async def stores(tmp_path):
    """All available implementations; teardown closes what needs closing."""
    collected: list[tuple[str, object]] = [("in_memory", InMemoryStorage())]
    sqlite = SqliteStorage(str(tmp_path / "contract.db"))
    collected.append(("sqlite", sqlite))
    sa_engine = None
    try:
        from sqlalchemy.ext.asyncio import create_async_engine

        from steerable_agent_runtime.storage import SqlAlchemyStorage

        sa_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sa.db'}")
        sa = SqlAlchemyStorage(sa_engine)
        await sa.create_all()
        collected.append(("sqlalchemy", sa))
    except ImportError:
        pass  # optional extra not installed — contract runs without it
    yield collected
    sqlite.close()
    if sa_engine is not None:
        await sa_engine.dispose()


@pytest.mark.asyncio
async def test_contract_list_messages_limit_takes_tail(stores) -> None:
    for name, store in stores:
        for i in range(5):
            await store.append_message(
                _message(f"m{i}", "c1", f"content {i}", f"2026-08-30T0{i}:00:00Z")
            )
        tail = await store.list_messages("c1", limit=2)
        assert [m.id for m in tail] == ["m3", "m4"], name


@pytest.mark.asyncio
async def test_contract_list_history_seq_bounds(stores) -> None:
    entries = [{"seq": i, "kind": "item", "data": {"i": i}} for i in range(6)]
    for name, store in stores:
        await store.append_history("r1", entries)
        # after_seq EXCLUSIVE: entries strictly after seq 2.
        after = await store.list_history("r1", after_seq=2)
        assert [e["seq"] for e in after] == [3, 4, 5], name
        # until_seq inclusive.
        until = await store.list_history("r1", until_seq=2)
        assert [e["seq"] for e in until] == [0, 1, 2], name
        # reverse + limit = newest-first tail scan.
        tail = await store.list_history("r1", reverse=True, limit=2)
        assert [e["seq"] for e in tail] == [5, 4], name


@pytest.mark.asyncio
async def test_contract_search_sessions_content_substring(stores) -> None:
    for name, store in stores:
        await store.upsert_session(
            _session("s1", "c1", updated_at="2026-08-30T01:00:00Z")
        )
        await store.upsert_session(
            _session("s2", "c2", user_id="u2", updated_at="2026-08-30T02:00:00Z")
        )
        await store.upsert_session(
            _session("s3", "c3", updated_at="2026-08-30T03:00:00Z")
        )
        await store.append_message(
            _message("m1", "c1", "deploy the release", "2026-08-30T01:00:00Z")
        )
        await store.append_message(
            _message("m2", "c2", "deploy the hotfix", "2026-08-30T02:00:00Z")
        )
        await store.append_message(
            _message("m3", "c3", "unrelated chat", "2026-08-30T03:00:00Z")
        )

        hits = await store.search_sessions("deploy")
        assert [s.sessionId for s in hits] == ["s2", "s1"], name  # updatedAt DESC

        scoped = await store.search_sessions("deploy", user_id="u1")
        assert [s.sessionId for s in scoped] == ["s1"], name

        assert await store.search_sessions("no-such-text") == [], name


@pytest.mark.asyncio
async def test_contract_list_history_records_with_prefix(stores) -> None:
    for name, store in stores:
        await store.append_history("chat/a", [{"seq": 0, "kind": "item", "data": {}}])
        await store.append_history("chat/b", [{"seq": 0, "kind": "item", "data": {}}])
        await store.append_history("other/c", [{"seq": 0, "kind": "item", "data": {}}])

        all_ids = await store.list_history_records()
        assert sorted(all_ids) == ["chat/a", "chat/b", "other/c"], name

        prefixed = await store.list_history_records(prefix="chat/")
        assert sorted(prefixed) == ["chat/a", "chat/b"], name
