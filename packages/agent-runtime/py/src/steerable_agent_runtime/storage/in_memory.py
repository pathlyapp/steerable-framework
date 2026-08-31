"""Reference in-memory StorageAdapter (default for sidecar / dev)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from copy import deepcopy
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


class InMemoryStorage:
    """Thread-safe in-memory storage. All mutations happen under an asyncio
    lock so concurrent dispatch from a single event loop is safe."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sessions: dict[str, AgentSession] = {}
        self._agents: dict[str, ChatAgent] = {}
        self._messages: dict[str, list[ChatMessage]] = {}
        self._traces: dict[str, HarnessTrace] = {}
        self._spans: dict[str, list[TraceSpan]] = {}
        self._events: dict[str, list[TraceEvent]] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def upsert_session(self, session: AgentSession) -> AgentSession:
        async with self._lock:
            self._sessions[session.sessionId] = deepcopy(session)
            return deepcopy(session)

    async def get_session(self, session_id: str) -> AgentSession | None:
        async with self._lock:
            value = self._sessions.get(session_id)
            return deepcopy(value) if value else None

    async def list_sessions(
        self,
        *,
        user_id: str | None = None,
        chat_id: str | None = None,
        active_only: bool = False,
    ) -> list[AgentSession]:
        async with self._lock:
            sessions = list(self._sessions.values())
        if user_id is not None:
            sessions = [s for s in sessions if s.userId == user_id]
        if chat_id is not None:
            sessions = [s for s in sessions if s.chatId == chat_id]
        if active_only:
            sessions = [s for s in sessions if s.isActive]
        sessions.sort(key=lambda s: s.updatedAt, reverse=True)
        return [deepcopy(s) for s in sessions]

    async def search_sessions(
        self, query: str, *, user_id: str | None = None
    ) -> list[AgentSession]:
        """Substring match over the serialized message — same semantics as
        the SQL LIKE in SqliteStorage, so contract tests can pin parity."""
        async with self._lock:
            hit_chat_ids = {
                chat_id
                for chat_id, bucket in self._messages.items()
                if any(query in m.model_dump_json() for m in bucket)
            }
            sessions = [
                s
                for s in self._sessions.values()
                if s.chatId in hit_chat_ids
                and (user_id is None or s.userId == user_id)
            ]
            sessions.sort(key=lambda s: s.updatedAt, reverse=True)
            return [deepcopy(s) for s in sessions]

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------

    async def upsert_agent(self, agent: ChatAgent) -> ChatAgent:
        async with self._lock:
            self._agents[agent.id] = deepcopy(agent)
            return deepcopy(agent)

    async def get_agent(self, agent_id: str) -> ChatAgent | None:
        async with self._lock:
            value = self._agents.get(agent_id)
            return deepcopy(value) if value else None

    async def list_agents(self, *, include_archived: bool = False) -> list[ChatAgent]:
        async with self._lock:
            agents = list(self._agents.values())
        if not include_archived:
            agents = [a for a in agents if not a.isArchived]
        agents.sort(key=lambda a: (a.sortOrder, a.createdAt))
        return [deepcopy(a) for a in agents]

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def append_message(self, message: ChatMessage) -> ChatMessage:
        if not message.chatId:
            raise StorageError("ChatMessage.chatId is required for append_message")
        async with self._lock:
            bucket = self._messages.setdefault(message.chatId, [])
            bucket.append(deepcopy(message))
            return deepcopy(message)

    async def list_messages(
        self, chat_id: str, *, limit: int | None = None
    ) -> list[ChatMessage]:
        async with self._lock:
            bucket = list(self._messages.get(chat_id, []))
        bucket.sort(key=lambda m: m.createdAt)
        if limit is not None:
            bucket = bucket[-limit:]
        return [deepcopy(m) for m in bucket]

    # ------------------------------------------------------------------
    # Traces / spans / events
    # ------------------------------------------------------------------

    async def upsert_trace(self, trace: HarnessTrace) -> HarnessTrace:
        async with self._lock:
            self._traces[trace.traceId] = deepcopy(trace)
            return deepcopy(trace)

    async def get_trace(self, trace_id: str) -> HarnessTrace | None:
        async with self._lock:
            value = self._traces.get(trace_id)
            return deepcopy(value) if value else None

    async def append_spans(self, trace_id: str, spans: Iterable[TraceSpan]) -> None:
        async with self._lock:
            bucket = self._spans.setdefault(trace_id, [])
            for span in spans:
                bucket.append(deepcopy(span))
            trace = self._traces.get(trace_id)
            if trace is not None:
                trace.spanCount = len(bucket)

    async def list_spans(self, trace_id: str) -> list[TraceSpan]:
        async with self._lock:
            return [deepcopy(span) for span in self._spans.get(trace_id, [])]

    async def append_events(self, trace_id: str, events: Iterable[TraceEvent]) -> None:
        async with self._lock:
            bucket = self._events.setdefault(trace_id, [])
            for event in events:
                bucket.append(deepcopy(event))
            trace = self._traces.get(trace_id)
            if trace is not None:
                trace.eventCount = len(bucket)

    async def list_events(self, trace_id: str) -> list[TraceEvent]:
        async with self._lock:
            return sorted(
                [deepcopy(event) for event in self._events.get(trace_id, [])],
                key=lambda event: event.sequence,
            )

    # ------------------------------------------------------------------
    # History record (Wave 1)
    # ------------------------------------------------------------------

    async def append_history(
        self, record_id: str, entries: Iterable[dict[str, Any]]
    ) -> None:
        async with self._lock:
            bucket = self._history.setdefault(record_id, [])
            for entry in entries:
                bucket.append(deepcopy(entry))

    async def list_history(
        self,
        record_id: str,
        *,
        after_seq: int | None = None,
        until_seq: int | None = None,
        limit: int | None = None,
        reverse: bool = False,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            bucket = [deepcopy(e) for e in self._history.get(record_id, [])]
        bucket.sort(key=lambda e: e.get("seq", 0), reverse=reverse)
        if after_seq is not None:
            bucket = [e for e in bucket if e.get("seq", 0) > after_seq]
        if until_seq is not None:
            bucket = [e for e in bucket if e.get("seq", 0) <= until_seq]
        if limit is not None:
            bucket = bucket[:limit]
        return bucket

    async def list_history_records(self, *, prefix: str | None = None) -> list[str]:
        """Enumerate known record ids. Optional extension beyond the
        StorageAdapter protocol — branch discovery (``agent.session.branches``)
        uses it when present and degrades to lineage-only without it."""
        async with self._lock:
            ids = list(self._history.keys())
        if prefix is not None:
            ids = [record_id for record_id in ids if record_id.startswith(prefix)]
        return sorted(ids)
