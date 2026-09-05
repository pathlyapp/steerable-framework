"""Zero-dependency SQLite StorageAdapter (W2.6.1).

Why stdlib ``sqlite3`` instead of the existing SQLAlchemy adapter: the
sidecar ships embedded in the desktop app under a CI-enforced bundle budget,
and SQLAlchemy is an optional dependency the embedded runtime does not carry.
The standard library's ``sqlite3`` is always present in the embedded
interpreter, so durable sessions cost zero dependency weight — the same
trade-off as the hand-rolled OTLP exporter (docs/spec/runtime.md
"Observability export decision").

Schema: one row per entity with the full pydantic JSON in a ``data`` column;
the columns used for filtering/ordering (ids, chat_id, seq, created_at) are
duplicated as real columns with indexes, so session enumeration and the
resume tail-scan are indexed lookups, never a table scan over JSON.

Concurrency: like ``InMemoryStorage``, all mutations run under one asyncio
lock (safe under concurrent ``await`` in a single loop). A sibling
``*.lock`` file (``fcntl.flock`` / Windows named mutex) makes the writer
process-exclusive: a second process opening the same path fails loud
(``StoreAlreadyOwnedError``). Process death releases the kernel lock;
there is no TTL steal; the lock file is never deleted. WAL remains so a
reader (e.g. the maintenance CLI) does not block the writer and does not
take the write lease.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from steerable_agent_protocol.generated import (
    AgentSession,
    ChatAgent,
    ChatMessage,
    HarnessTrace,
    TraceEvent,
    TraceSpan,
)

from ..errors import StorageError
from .write_lease import WriteLease, acquire_write_lease

_SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    chat_id    TEXT NOT NULL,
    is_active  INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    data       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_chat ON sessions(chat_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, updated_at);

CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    is_archived INTEGER NOT NULL DEFAULT 0,
    data        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,
    chat_id    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, created_at);

CREATE TABLE IF NOT EXISTS traces (
    trace_id   TEXT PRIMARY KEY,
    session_id TEXT,
    chat_id    TEXT,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_chat ON traces(chat_id, created_at);

CREATE TABLE IF NOT EXISTS spans (
    trace_id TEXT NOT NULL,
    data     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);

CREATE TABLE IF NOT EXISTS events (
    trace_id TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    data     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_trace ON events(trace_id, seq);

CREATE TABLE IF NOT EXISTS history (
    record_id TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    data      TEXT NOT NULL,
    PRIMARY KEY (record_id, seq)
);
"""


def _dump(model: Any) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False)


class SqliteStorage:
    """Stdlib-sqlite3 StorageAdapter. See module docstring for the contract."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._lease: WriteLease = acquire_write_lease(path)
        try:
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.executescript(_SCHEMA)
        except BaseException:
            self._lease.release()
            raise

    def close(self) -> None:
        self._db.close()
        self._lease.release()

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def upsert_session(self, session: AgentSession) -> AgentSession:
        async with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO sessions"
                " (session_id, user_id, chat_id, is_active, updated_at, data)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session.sessionId,
                    session.userId,
                    session.chatId,
                    int(session.isActive),
                    session.updatedAt,
                    _dump(session),
                ),
            )
            self._db.commit()
        return session

    async def get_session(self, session_id: str) -> AgentSession | None:
        async with self._lock:
            row = self._db.execute(
                "SELECT data FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return AgentSession.model_validate_json(row["data"]) if row else None

    async def list_sessions(
        self,
        *,
        user_id: str | None = None,
        chat_id: str | None = None,
        active_only: bool = False,
    ) -> list[AgentSession]:
        sql = "SELECT data FROM sessions"
        clauses: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if chat_id is not None:
            clauses.append("chat_id = ?")
            params.append(chat_id)
        if active_only:
            clauses.append("is_active = 1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC"
        async with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [AgentSession.model_validate_json(r["data"]) for r in rows]

    async def search_sessions(
        self, query: str, *, user_id: str | None = None
    ) -> list[AgentSession]:
        """Sessions whose chat has a message containing ``query``.

        Implementation-specific capability beyond the StorageAdapter
        protocol (W2.6.1): content search is a SQL LIKE over the messages
        table joined back to sessions — no jsonl scanning, no full load.
        """
        async with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT s.data FROM sessions s"
                " JOIN messages m ON m.chat_id = s.chat_id"
                " WHERE m.data LIKE ?" + (" AND s.user_id = ?" if user_id else "")
                + " ORDER BY s.updated_at DESC",
                [f"%{query}%", *([user_id] if user_id else [])],
            ).fetchall()
        return [AgentSession.model_validate_json(r["data"]) for r in rows]

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------

    async def upsert_agent(self, agent: ChatAgent) -> ChatAgent:
        async with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO agents (id, is_archived, data) VALUES (?, ?, ?)",
                (agent.id, int(bool(agent.isArchived)), _dump(agent)),
            )
            self._db.commit()
        return agent

    async def get_agent(self, agent_id: str) -> ChatAgent | None:
        async with self._lock:
            row = self._db.execute(
                "SELECT data FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
        return ChatAgent.model_validate_json(row["data"]) if row else None

    async def list_agents(self, *, include_archived: bool = False) -> list[ChatAgent]:
        sql = "SELECT data FROM agents"
        if not include_archived:
            sql += " WHERE is_archived = 0"
        async with self._lock:
            rows = self._db.execute(sql).fetchall()
        agents = [ChatAgent.model_validate_json(r["data"]) for r in rows]
        # Parity with InMemoryStorage: (sortOrder, createdAt); None sorts last.
        agents.sort(key=lambda a: (a.sortOrder is None, a.sortOrder or 0, a.createdAt))
        return agents

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def append_message(self, message: ChatMessage) -> ChatMessage:
        if not message.chatId:
            raise StorageError("ChatMessage.chatId is required for append_message")
        async with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO messages (id, chat_id, created_at, data)"
                " VALUES (?, ?, ?, ?)",
                (message.id, message.chatId, message.createdAt, _dump(message)),
            )
            self._db.commit()
        return message

    async def list_messages(
        self, chat_id: str, *, limit: int | None = None
    ) -> list[ChatMessage]:
        # ``limit`` keeps the NEWEST N (tail semantics, like InMemoryStorage):
        # select newest-first with the limit, then re-order ascending.
        if limit is not None:
            async with self._lock:
                rows = self._db.execute(
                    "SELECT data FROM messages WHERE chat_id = ?"
                    " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (chat_id, limit),
                ).fetchall()
            rows.reverse()
        else:
            async with self._lock:
                rows = self._db.execute(
                    "SELECT data FROM messages WHERE chat_id = ?"
                    " ORDER BY created_at, rowid",
                    (chat_id,),
                ).fetchall()
        return [ChatMessage.model_validate_json(r["data"]) for r in rows]

    # ------------------------------------------------------------------
    # Traces + spans + events
    # ------------------------------------------------------------------

    async def upsert_trace(self, trace: HarnessTrace) -> HarnessTrace:
        async with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO traces"
                " (trace_id, session_id, chat_id, status, created_at, data)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    trace.traceId,
                    trace.sessionId,
                    trace.chatId,
                    trace.status,
                    trace.createdAt,
                    _dump(trace),
                ),
            )
            self._db.commit()
        return trace

    async def get_trace(self, trace_id: str) -> HarnessTrace | None:
        async with self._lock:
            row = self._db.execute(
                "SELECT data FROM traces WHERE trace_id = ?", (trace_id,)
            ).fetchone()
        return HarnessTrace.model_validate_json(row["data"]) if row else None

    async def append_spans(self, trace_id: str, spans: Iterable[TraceSpan]) -> None:
        rows = [(trace_id, _dump(s)) for s in spans]
        if not rows:
            return
        async with self._lock:
            self._db.executemany(
                "INSERT INTO spans (trace_id, data) VALUES (?, ?)", rows
            )
            # Parity with InMemoryStorage: the trace row's spanCount tracks
            # the number of persisted spans.
            count = self._db.execute(
                "SELECT COUNT(*) AS n FROM spans WHERE trace_id = ?", (trace_id,)
            ).fetchone()["n"]
            self._bump_trace_count(trace_id, "spanCount", count)
            self._db.commit()

    async def list_spans(self, trace_id: str) -> list[TraceSpan]:
        async with self._lock:
            rows = self._db.execute(
                "SELECT data FROM spans WHERE trace_id = ? ORDER BY rowid", (trace_id,)
            ).fetchall()
        return [TraceSpan.model_validate_json(r["data"]) for r in rows]

    async def append_events(self, trace_id: str, events: Iterable[TraceEvent]) -> None:
        rows = [(trace_id, e.sequence, _dump(e)) for e in events]
        if not rows:
            return
        async with self._lock:
            self._db.executemany(
                "INSERT INTO events (trace_id, seq, data) VALUES (?, ?, ?)", rows
            )
            count = self._db.execute(
                "SELECT COUNT(*) AS n FROM events WHERE trace_id = ?", (trace_id,)
            ).fetchone()["n"]
            self._bump_trace_count(trace_id, "eventCount", count)
            self._db.commit()

    def _bump_trace_count(self, trace_id: str, field: str, count: int) -> None:
        """Read-modify-write the trace row's counter (caller holds the lock)."""
        row = self._db.execute(
            "SELECT data FROM traces WHERE trace_id = ?", (trace_id,)
        ).fetchone()
        if row is None:
            return
        trace = HarnessTrace.model_validate_json(row["data"])
        setattr(trace, field, count)
        self._db.execute(
            "UPDATE traces SET data = ? WHERE trace_id = ?", (_dump(trace), trace_id)
        )

    async def list_events(self, trace_id: str) -> list[TraceEvent]:
        async with self._lock:
            rows = self._db.execute(
                "SELECT data FROM events WHERE trace_id = ? ORDER BY seq", (trace_id,)
            ).fetchall()
        return [TraceEvent.model_validate_json(r["data"]) for r in rows]

    # ------------------------------------------------------------------
    # History record (Wave 1)
    # ------------------------------------------------------------------

    async def append_history(
        self, record_id: str, entries: Iterable[dict[str, Any]]
    ) -> None:
        rows = [
            (record_id, int(e["seq"]), json.dumps(e, ensure_ascii=False))
            for e in entries
        ]
        if not rows:
            return
        async with self._lock:
            self._db.executemany(
                "INSERT INTO history (record_id, seq, data) VALUES (?, ?, ?)", rows
            )
            self._db.commit()

    async def list_history(
        self,
        record_id: str,
        *,
        after_seq: int | None = None,
        until_seq: int | None = None,
        limit: int | None = None,
        reverse: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT data FROM history WHERE record_id = ?"
        params: list[Any] = [record_id]
        if after_seq is not None:
            # Exclusive lower bound (parity with InMemoryStorage): entries
            # strictly after ``after_seq``.
            sql += " AND seq > ?"
            params.append(after_seq)
        if until_seq is not None:
            sql += " AND seq <= ?"
            params.append(until_seq)
        sql += " ORDER BY seq " + ("DESC" if reverse else "ASC")
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        async with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [json.loads(r["data"]) for r in rows]

    async def list_history_records(self, *, prefix: str | None = None) -> list[str]:
        """Enumerate known record ids — the branch-discovery extension
        (``agent.session.branches``), parity with InMemoryStorage."""
        sql = "SELECT DISTINCT record_id FROM history"
        params: list[Any] = []
        if prefix is not None:
            sql += " WHERE record_id LIKE ?"
            params.append(f"{prefix}%")
        sql += " ORDER BY record_id"
        async with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [r["record_id"] for r in rows]
