"""Maintenance jobs for SqliteStorage databases (W2.6.2)."""

from __future__ import annotations

import json

import pytest
from steerable_agent_protocol.generated import (
    AgentSession,
    ChatMessage,
    HarnessTrace,
)
from steerable_agent_runtime.maintenance import (
    archive,
    check,
    compact,
    main,
    salvage,
)
from steerable_agent_runtime.storage import SqliteStorage


def _session(session_id: str, chat_id: str, updated_at: str) -> AgentSession:
    return AgentSession(
        sessionId=session_id,
        userId="u1",
        chatId=chat_id,
        currentStage="chat",
        isActive=True,
        createdAt=updated_at,
        updatedAt=updated_at,
    )


def _trace(trace_id: str, created_at: str) -> HarnessTrace:
    return HarnessTrace(
        traceId=trace_id,
        chatId="c1",
        status="completed",
        hadError=False,
        eventCount=0,
        spanCount=0,
        createdAt=created_at,
        updatedAt=created_at,
    )


@pytest.mark.asyncio
async def test_check_ok_on_healthy_db(tmp_path) -> None:
    path = str(tmp_path / "ok.db")
    SqliteStorage(path).close()
    report = check(path)
    assert report.ok is True


@pytest.mark.asyncio
async def test_compact_removes_only_old_traces(tmp_path) -> None:
    path = str(tmp_path / "c.db")
    store = SqliteStorage(path)
    await store.upsert_trace(_trace("t_old", "2020-01-01T00:00:00Z"))
    await store.upsert_trace(_trace("t_new", "2999-01-01T00:00:00Z"))
    await store.upsert_session(_session("s1", "c1", "2020-01-01T00:00:00Z"))
    store.close()

    report = compact(path, older_than_days=30)
    assert report.ok is True
    assert report.details["traces_removed"] == 1

    reopened = SqliteStorage(path)
    assert await reopened.get_trace("t_old") is None
    assert await reopened.get_trace("t_new") is not None
    # sessions are never compacted away
    assert await reopened.get_session("s1") is not None
    reopened.close()


@pytest.mark.asyncio
async def test_archive_moves_old_sessions_with_messages(tmp_path) -> None:
    path = str(tmp_path / "main.db")
    arch_path = str(tmp_path / "archive.db")
    store = SqliteStorage(path)
    await store.upsert_session(_session("s_old", "c_old", "2020-01-01T00:00:00Z"))
    await store.upsert_session(_session("s_new", "c_new", "2999-01-01T00:00:00Z"))
    await store.append_message(
        ChatMessage(
            id="m1",
            chatId="c_old",
            role="user",
            content="old chat",
            createdAt="2020-01-01T00:00:00Z",
        )
    )
    store.close()

    report = archive(path, arch_path, older_than_days=30)
    assert report.ok is True
    assert report.details["sessions_archived"] == 1
    assert report.details["messages_archived"] == 1

    main_store = SqliteStorage(path)
    assert await main_store.get_session("s_old") is None
    assert await main_store.get_session("s_new") is not None
    assert await main_store.list_messages("c_old") == []
    main_store.close()

    # the archive is itself a readable SqliteStorage database
    arch_store = SqliteStorage(arch_path)
    archived = await arch_store.get_session("s_old")
    assert archived is not None and archived.chatId == "c_old"
    assert [m.content for m in await arch_store.list_messages("c_old")] == ["old chat"]
    arch_store.close()


@pytest.mark.asyncio
async def test_salvage_exports_decodable_rows(tmp_path) -> None:
    path = str(tmp_path / "s.db")
    out = tmp_path / "salvage.jsonl"
    store = SqliteStorage(path)
    await store.upsert_session(_session("s1", "c1", "2026-08-30T00:00:00Z"))
    store.close()

    report = salvage(path, str(out))
    assert report.ok is True
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert any(
        r["table"] == "sessions" and r["row"]["session_id"] == "s1" for r in rows
    )
    assert report.details["rows_written"] >= 1


def test_cli_check(tmp_path, capsys) -> None:
    path = str(tmp_path / "cli.db")
    SqliteStorage(path).close()
    assert main([path, "check"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"job": "check", "ok": True}
